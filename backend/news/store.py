"""news.db — scraped headlines, where they came from, what Claude made of them, and what price did.

Two timestamps per item, on purpose:
  published  what the source says (can be edited or backdated) — used for display and decay
  seen       when this app first read it — used for anything that looks at price afterwards,
             so the research never credits a headline with a move that happened before we knew
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

from storage.db import DATA_DIR, connect

NEWS_DB = os.getenv("NEWS_DB", os.path.join(DATA_DIR, "news.db"))

_lock = threading.Lock()
_con = None
_path = NEWS_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT, source TEXT, summary TEXT,
    published REAL, seen REAL NOT NULL, sources TEXT NOT NULL DEFAULT '[]', mentions TEXT NOT NULL DEFAULT '[]',
    coins TEXT, scope TEXT, event_type TEXT, direction REAL, importance INTEGER, note TEXT,
    classified_at REAL, vader REAL
);
CREATE INDEX IF NOT EXISTS news_seen ON news_items(seen);
CREATE INDEX IF NOT EXISTS news_pending ON news_items(classified_at);
CREATE TABLE IF NOT EXISTS news_sources (
    name TEXT PRIMARY KEY, last_poll REAL, last_ok REAL, last_error TEXT,
    items_total INTEGER DEFAULT 0, new_last INTEGER DEFAULT 0, stale_dropped INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS news_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, trigger TEXT, mood TEXT, verdict TEXT,
    headlines TEXT, inputs_key TEXT
);
CREATE TABLE IF NOT EXISTS news_reactions (
    item_id TEXT NOT NULL, symbol TEXT NOT NULL, p0 REAL,
    r_1h REAL, r_4h REAL, r_24h REAL, labelled_at REAL,
    PRIMARY KEY (item_id, symbol)
);
"""


def use(path: str) -> None:
    """Point at another database (tests)."""
    global _con, _path
    with _lock:
        _con, _path = None, path


def _db():
    global _con
    if _con is None:
        _con = connect(_path)
        _con.executescript(SCHEMA)
        # Columns added after the first release.
        for sql in ("ALTER TABLE news_sources ADD COLUMN newest REAL",
                    "ALTER TABLE news_items ADD COLUMN story TEXT",            # id of the story's first item
                    "ALTER TABLE news_items ADD COLUMN story_checked INTEGER"):  # Claude has checked the link
            try:
                _con.execute(sql)
            except Exception:
                pass
    return _con


def _rows(sql: str, params=()) -> list[dict]:
    with _lock:
        cur = _db().execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


_NORM = re.compile(r"[^a-z0-9]+")


def item_id(title: str) -> str:
    """Same story from two outlets usually carries the same headline; normalise and hash it."""
    return hashlib.sha1(_NORM.sub(" ", title.lower()).strip().encode()).hexdigest()[:20]


# ── Stories: one event, many headlines ─────────────────────────────────────────
# "SEC sues X over $15M scam" and "SEC Sues X Over Alleged $15 Million Scam" hash differently
# but are one event; so are fourteen headlines about one coin's rally. Each headline carries a
# `story` (the id of the story's first headline) and scoring, bursts and reaction research count
# a story once. Word overlap catches rewordings here at intake; Claude links the rest when it
# classifies (classify.py, "same_story_as").

STORY_WINDOW = 36 * 3600
SIMILAR = 0.55               # Jaccard on content words
_STOP = set("""a an the and or of to in on for at by with from as is are was were be been it its this that
    after amid over into up down out about than vs via new says say said report reports could would will
    can may might just now today why how what who more most""".split())
_WORD = re.compile(r"[a-z0-9]+")


def words(title: str) -> set[str]:
    out = set()
    for w in _WORD.findall(title.lower().replace("million", "m").replace("billion", "b")):
        if w in _STOP or len(w) < 2:
            continue
        out.add(w.rstrip("s") if len(w) > 3 else w)
    return out


def similar(a: set[str], b: set[str]) -> bool:
    shared = len(a & b)
    return shared >= 3 and shared / len(a | b) >= SIMILAR


_version = 0                 # bumped on every write — read-side caches key on it


def version() -> int:
    return _version


def _bump() -> None:
    global _version
    _version += 1


def add(items: list[dict]) -> list[dict]:
    """Insert new items; for known ones, record the extra source. Returns only the new items."""
    new = []
    with _lock:
        con = _db()
        recent = None
        for it in items:
            iid = item_id(it["title"])
            row = con.execute("SELECT sources FROM news_items WHERE id=?", (iid,)).fetchone()
            ref = {"source": it.get("publisher") or it["source"], "via": it["source"], "url": it.get("url"),
                   "published": it.get("published")}
            if row:
                srcs = json.loads(row[0])
                if not any(s["source"] == ref["source"] for s in srcs):
                    srcs.append(ref)
                    con.execute("UPDATE news_items SET sources=? WHERE id=?", (json.dumps(srcs), iid))
                continue
            if recent is None:
                recent = [(r[0], r[1] or r[0], words(r[2])) for r in con.execute(
                    "SELECT id, story, title FROM news_items WHERE seen >= ?", (it["seen"] - STORY_WINDOW,))]
            w = words(it["title"])
            story = next((st for _, st, ws in recent if similar(w, ws)), iid)
            con.execute(
                "INSERT INTO news_items (id, title, url, source, summary, published, seen, sources, mentions, vader, story) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (iid, it["title"], it.get("url"), ref["source"], it.get("summary") or "", it.get("published"),
                 it["seen"], json.dumps([ref]), json.dumps(it.get("mentions") or []), it.get("vader"), story))
            recent.append((iid, story, w))
            new.append({**it, "id": iid, "story": story})
        con.commit()
        if new:
            _bump()
    return new


def set_stories(links: dict[str, str]) -> None:
    """{item id: story id} — Claude's "same event" links; also marks the items as checked."""
    with _lock:
        con = _db()
        for iid, story in links.items():
            con.execute("UPDATE news_items SET story=?, story_checked=1 WHERE id=?", (story, iid))
        con.commit()
        _bump()


def merge_stories(target: str, others: list[str]) -> int:
    """Fold `others` into story `target` (every headline of theirs now belongs to it)."""
    if not others:
        return 0
    q = ",".join("?" * len(others))
    with _lock:
        con = _db()
        n = con.execute(f"UPDATE news_items SET story=? WHERE COALESCE(story, id) IN ({q})", (target, *others)).rowcount
        con.commit()
        _bump()
    return n


def recent_stories(since: float, coins: set[str] | None = None, limit: int = 60) -> list[dict]:
    """One line per recent classified story (its lead headline) — the context Claude links against."""
    rows = _rows("SELECT id, COALESCE(story, id) AS story, title, coins, scope, importance, seen FROM news_items "
                 "WHERE classified_at IS NOT NULL AND scope != 'irrelevant' AND seen >= ? ORDER BY seen DESC", (since,))
    out, seen_story = [], set()
    for r in rows:
        if r["story"] in seen_story:
            continue
        cs = set(json.loads(r["coins"] or "[]"))
        if coins is not None and cs and not cs & coins:
            continue
        seen_story.add(r["story"])
        out.append({"story": r["story"], "title": r["title"], "coins": sorted(cs)})
        if len(out) >= limit:
            break
    return out


def unchecked_stories(since: float, limit: int) -> list[dict]:
    """Classified items whose story link Claude hasn't confirmed (collected before linking existed)."""
    return [_decode(r) for r in _rows(
        "SELECT * FROM news_items WHERE classified_at IS NOT NULL AND story_checked IS NULL AND scope != 'irrelevant' "
        "AND seen >= ? ORDER BY seen LIMIT ?", (since, limit))]


MAX_AGE = 48 * 3600          # older than this, a headline is no longer classified or scored


def pending(limit: int) -> list[dict]:
    """Unclassified items still worth classifying: Binance announcements first, then newest first
    (under a backlog the freshest news matters most; anything past MAX_AGE is simply left)."""
    return [_decode(r) for r in _rows(
        "SELECT * FROM news_items WHERE classified_at IS NULL AND seen >= ? "
        "ORDER BY (source LIKE 'Binance%') DESC, seen DESC LIMIT ?", (time.time() - MAX_AGE, limit))]


def oldest_pending_seen() -> float | None:
    return _rows("SELECT MIN(seen) AS s FROM news_items WHERE classified_at IS NULL AND seen >= ?",
                 (time.time() - MAX_AGE,))[0]["s"]


def pending_count() -> int:
    return _rows("SELECT COUNT(*) AS n FROM news_items WHERE classified_at IS NULL AND seen >= ?",
                 (time.time() - MAX_AGE,))[0]["n"]


def save_labels(labels: dict[str, dict]) -> None:
    now = time.time()
    with _lock:
        con = _db()
        for iid, a in labels.items():
            con.execute(
                "UPDATE news_items SET coins=?, scope=?, event_type=?, direction=?, importance=?, note=?, classified_at=? "
                "WHERE id=?",
                (json.dumps(a.get("coins") or []), a.get("scope"), a.get("event_type"), a.get("direction"),
                 a.get("importance"), a.get("note"), now, iid))
            if a.get("story"):
                con.execute("UPDATE news_items SET story=?, story_checked=1 WHERE id=?", (a["story"], iid))
        con.commit()
        _bump()


def _decode(r: dict) -> dict:
    r["story"] = r.get("story") or r.get("id")
    r["sources"] = json.loads(r.get("sources") or "[]")
    r["mentions"] = json.loads(r.get("mentions") or "[]")
    r["coins"] = json.loads(r["coins"]) if r.get("coins") else None
    return r


def items(since: float, limit: int = 5000) -> list[dict]:
    """Items first seen since `since`, newest first."""
    return [_decode(r) for r in _rows("SELECT * FROM news_items WHERE seen >= ? ORDER BY seen DESC LIMIT ?",
                                      (since, limit))]


def one(iid: str) -> dict | None:
    rows = _rows("SELECT * FROM news_items WHERE id=?", (iid,))
    return _decode(rows[0]) if rows else None


def first_seen() -> float | None:
    """When collection started — the burst baseline needs days of history."""
    return _rows("SELECT MIN(seen) AS s FROM news_items")[0]["s"]


def seen_counts(since: float) -> list[dict]:
    """(coin, seen, story) for burst baselines — cheap, no text."""
    return _rows("SELECT seen, coins, mentions, COALESCE(story, id) AS story FROM news_items WHERE seen >= ? "
                 "AND (scope IS NULL OR scope != 'irrelevant')", (since,))


# ── Source health ──────────────────────────────────────────────────────────────

def source_polled(name: str, ok: bool, new: int = 0, stale: int = 0, error: str | None = None,
                  newest: float | None = None) -> None:
    now = time.time()
    with _lock:
        con = _db()
        con.execute("INSERT OR IGNORE INTO news_sources (name) VALUES (?)", (name,))
        if ok:
            # `newest` is the youngest publish date the feed carried — a feed stuck months back shows here.
            con.execute("UPDATE news_sources SET last_poll=?, last_ok=?, last_error=NULL, new_last=?, "
                        "items_total=items_total+?, stale_dropped=stale_dropped+?, "
                        "newest=MAX(COALESCE(newest, 0), COALESCE(?, 0)) WHERE name=?",
                        (now, now, new, new, stale, newest, name))
        else:
            con.execute("UPDATE news_sources SET last_poll=?, last_error=? WHERE name=?", (now, (error or "")[:300], name))
        con.commit()


def sources() -> list[dict]:
    return _rows("SELECT * FROM news_sources ORDER BY name")


# ── Price reactions ────────────────────────────────────────────────────────────

def reactions_pending(ready_before: float, limit: int = 2000) -> list[dict]:
    """Classified items with coins (or market-wide) whose 1h horizon has passed and aren't fully labelled."""
    return [_decode(r) for r in _rows("""
        SELECT i.* FROM news_items i
        WHERE i.classified_at IS NOT NULL AND i.scope != 'irrelevant' AND i.seen <= ?
          AND i.seen >= ? AND NOT EXISTS (SELECT 1 FROM news_reactions x WHERE x.item_id = i.id AND x.r_24h IS NOT NULL)
        ORDER BY i.seen LIMIT ?""", (ready_before, time.time() - 3 * 86400, limit))]


def save_reactions(rows: list[tuple]) -> None:
    """rows: (item_id, symbol, p0, r_1h, r_4h, r_24h)."""
    now = time.time()
    with _lock:
        con = _db()
        con.executemany(
            "INSERT INTO news_reactions (item_id, symbol, p0, r_1h, r_4h, r_24h, labelled_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id, symbol) DO UPDATE SET p0=excluded.p0, r_1h=excluded.r_1h, r_4h=excluded.r_4h, "
            "r_24h=excluded.r_24h, labelled_at=excluded.labelled_at", [(*r, now) for r in rows])
        con.commit()


def reactions(item_ids: list[str] | None = None) -> list[dict]:
    if item_ids is None:
        return _rows("SELECT x.*, i.event_type, i.direction, i.importance, i.scope, i.seen, "
                     "COALESCE(i.story, i.id) AS story FROM news_reactions x JOIN news_items i ON i.id = x.item_id")
    if not item_ids:
        return []
    q = ",".join("?" * len(item_ids))
    return _rows(f"SELECT * FROM news_reactions WHERE item_id IN ({q})", tuple(item_ids))


# ── App headline editions ──────────────────────────────────────────────────────

def save_brief(trigger: str, mood: dict, verdict: dict, headlines: list[dict], inputs_key: str) -> int:
    with _lock:
        con = _db()
        cur = con.execute("INSERT INTO news_briefs (ts, trigger, mood, verdict, headlines, inputs_key) VALUES (?,?,?,?,?,?)",
                          (time.time(), trigger, json.dumps(mood), json.dumps(verdict), json.dumps(headlines), inputs_key))
        con.commit()
        _bump()
        return cur.lastrowid


def _brief_row(r: dict) -> dict:
    for k in ("mood", "verdict", "headlines"):
        r[k] = json.loads(r[k] or "null")
    return r


def brief(brief_id: int | None = None) -> dict | None:
    """One edition (the latest by default) with the ids either side of it, for paging."""
    rows = _rows("SELECT * FROM news_briefs WHERE id = ?", (brief_id,)) if brief_id else \
        _rows("SELECT * FROM news_briefs ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    b = _brief_row(rows[0])
    prev = _rows("SELECT id FROM news_briefs WHERE id < ? ORDER BY id DESC LIMIT 1", (b["id"],))
    nxt = _rows("SELECT id FROM news_briefs WHERE id > ? ORDER BY id LIMIT 1", (b["id"],))
    b["prev_id"] = prev[0]["id"] if prev else None
    b["next_id"] = nxt[0]["id"] if nxt else None
    b["latest"] = b["next_id"] is None
    return b


def briefs(limit: int = 50) -> list[dict]:
    """The edition timeline: when, why, the verdict and the lead headline."""
    out = []
    for r in _rows("SELECT id, ts, trigger, verdict, headlines FROM news_briefs ORDER BY id DESC LIMIT ?", (limit,)):
        v, h = json.loads(r["verdict"] or "{}"), json.loads(r["headlines"] or "[]")
        out.append({"id": r["id"], "ts": r["ts"], "trigger": r["trigger"], "label": v.get("label"),
                    "lead": h[0]["title"] if h else None})
    return out


def last_brief_meta() -> dict | None:
    rows = _rows("SELECT id, ts, inputs_key FROM news_briefs ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None
