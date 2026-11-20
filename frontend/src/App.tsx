import { useState, useCallback, useEffect, lazy, Suspense } from 'react'
import HeaderClock from './components/ui/Clock'
import { Zap, BarChart2, Briefcase, RefreshCw, AlertCircle, Activity, Clock, Radar, History, List, ShieldAlert, CandlestickChart, Microscope, Archive, Newspaper, Layers, Globe2, CalendarClock, Users, Crosshair, Trophy, FlaskConical, CreditCard, ShieldCheck, Lock } from 'lucide-react'
import { useAuth } from './auth/AuthProvider'
import { SignIn } from './auth/SignIn'
import { AccountMenu } from './components/account/AccountMenu'
import { AllSignals } from './components/AllSignals'
import { OracleSignal } from './components/OracleSignal'
import { SignalScores } from './components/SignalScores'
import { PriceChart } from './components/PriceChart'
import { PipelineStatus } from './components/PipelineStatus'
import { AlarmBanner } from './components/AlarmBanner'
import { DataWatchBanner } from './components/DataWatchBanner'
import { MarketStateChip } from './components/MarketStateChip'
import { CalendarChip } from './components/CalendarChip'
import { useOracle } from './hooks/useOracle'
import { useAlarm } from './hooks/useAlarm'
import { api } from './utils/api'
import type { SystemKey } from './types/overview'
import clsx from 'clsx'

// Tabs other than All Signals load on first open — the chart libraries alone are most of the bundle.
const TradeHistory = lazy(() => import('./components/TradeHistory').then(m => ({ default: m.TradeHistory })))
const TradesPage = lazy(() => import('./components/TradesPage').then(m => ({ default: m.TradesPage })))
const LivePage = lazy(() => import('./components/LivePage').then(m => ({ default: m.LivePage })))
const TradeTerminal = lazy(() => import('./components/trade/TradeTerminal').then(m => ({ default: m.TradeTerminal })))
const RetiredPage = lazy(() => import('./components/RetiredPage').then(m => ({ default: m.RetiredPage })))
const LabPage = lazy(() => import('./components/LabPage').then(m => ({ default: m.LabPage })))
const AuctionPage = lazy(() => import('./components/AuctionPage').then(m => ({ default: m.AuctionPage })))
const PropPage = lazy(() => import('./components/PropPage').then(m => ({ default: m.PropPage })))
const CompositePage = lazy(() => import('./components/CompositePage').then(m => ({ default: m.CompositePage })))
const ControlPage = lazy(() => import('./components/ControlPage').then(m => ({ default: m.ControlPage })))
const CopyPage = lazy(() => import('./components/CopyPage').then(m => ({ default: m.CopyPage })))
const MarketsPage = lazy(() => import('./components/MarketsPage').then(m => ({ default: m.MarketsPage })))
const BooksPage = lazy(() => import('./components/BooksPage').then(m => ({ default: m.BooksPage })))
const CalendarPage = lazy(() => import('./components/CalendarPage').then(m => ({ default: m.CalendarPage })))
const NewsPage = lazy(() => import('./components/NewsPage').then(m => ({ default: m.NewsPage })))
const Portfolio = lazy(() => import('./components/Portfolio').then(m => ({ default: m.Portfolio })))
const PricingPage = lazy(() => import('./components/PricingPage').then(m => ({ default: m.PricingPage })))
const AdminPage = lazy(() => import('./components/AdminPage').then(m => ({ default: m.AdminPage })))
const SettingsPanel = lazy(() => import('./components/account/SettingsPanel').then(m => ({ default: m.SettingsPanel })))

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT']
type Tab = 'all' | 'signal' | 'news' | 'calendar' | 'trade' | 'portfolio' | 'trades' | 'history' | 'markets' | 'copy' | 'mentor' | 'prop' | 'one' | 'control' | 'lab' | 'retired' | 'live' | 'books' | 'pricing' | 'admin'
type Focus = { system: SystemKey; id: string }

/**
 * Tab → feature key (docs/saas.md "Tab → feature"). A tab whose feature the tier lacks is shown locked and opens the
 * plans instead. `null` = everyone. all/signal are special: the free tier has `signals.delayed`, so they open but
 * carry a delay banner; they lock only when the tier has neither signals key.
 */
const TAB_FEATURE: Record<Tab, string | null> = {
  all: 'signals.live', signal: 'signals.live', news: 'news', calendar: 'calendar',
  trade: 'trades', trades: 'trades', history: 'trades', portfolio: 'trades', books: 'trades',
  markets: 'markets', copy: 'copy.view', mentor: 'mentor', prop: 'prop', one: 'one', control: 'control',
  lab: 'lab', retired: 'retired', live: 'mirror.exchange', pricing: null, admin: null,
}
const SIGNAL_TABS: Tab[] = ['all', 'signal']

/** The shell behind the gate: a session (or an ungated local instance) is guaranteed here. */
export default function App() {
  const auth = useAuth()
  if (auth.loading) {
    return (
      <div className="min-h-screen bg-oracle-bg text-gray-100 flex items-center justify-center">
        <div className="flex items-center gap-2 text-sm text-gray-500"><Activity className="w-4 h-4 animate-pulse text-blue-400" /> Loading…</div>
      </div>
    )
  }
  if (auth.config?.auth_required && !auth.session) return <SignIn />
  return <Shell />
}

function Shell() {
  const { session, has, isAdmin } = useAuth()
  // The tab lives in the URL hash (#trades, #history…) so views can be bookmarked and shared.
  const TABS: Tab[] = ['all', 'signal', 'news', 'calendar', 'trade', 'trades', 'history', 'markets', 'copy', 'mentor', 'prop', 'one', 'control', 'lab', 'retired', 'live', 'books', 'portfolio', 'pricing', 'admin']
  const [tab, setTabState] = useState<Tab>(() => {
    const h = window.location.hash.slice(1).split('/')[0] as Tab
    return TABS.includes(h) ? h : 'all'
  })
  const setTab = useCallback((t: Tab) => {
    setTabState(t)
    if (window.location.hash.slice(1).split('/')[0] !== t) window.history.replaceState(null, '', `#${t}`)
  }, [])
  // Tier gating: is a tab locked for this session, and which feature the plans page should light up (#pricing/<feature>).
  const locked = useCallback((t: Tab): string | null => {
    const f = TAB_FEATURE[t]
    if (!f) return null
    if (SIGNAL_TABS.includes(t)) return has('signals.live') || has('signals.delayed') ? null : 'signals.delayed'
    return has(f) ? null : f
  }, [has])
  const [wantFeature, setWantFeature] = useState<string | null>(() => {
    const [h, f] = window.location.hash.slice(1).split('/')
    return h === 'pricing' && f ? f : null
  })
  const openPricing = useCallback((feature?: string | null) => {
    setWantFeature(feature ?? null)
    setTabState('pricing')
    window.history.replaceState(null, '', feature ? `#pricing/${feature}` : '#pricing')
  }, [])
  const [settingsOpen, setSettingsOpen] = useState(false)
  const delayedSignals = SIGNAL_TABS.includes(tab) && !!session && has('signals.delayed') && !has('signals.live')
  // A trade followed on the Trade tab's chart lives in the hash too: #trade/<system>/<id> (opened from the Trades tab).
  const focusFromHash = (): Focus | null => {
    const [h, sys, id] = window.location.hash.slice(1).split('/')
    return h === 'trade' && sys && id ? { system: sys as SystemKey, id } : null
  }
  const [tradeFocus, setTradeFocus] = useState<Focus | null>(focusFromHash)
  const openOnChart = useCallback((system: SystemKey, id: string) => {
    setTradeFocus({ system, id })
    setTabState('trade')
    window.history.replaceState(null, '', `#trade/${system}/${id}`)
  }, [])
  const clearFocus = useCallback(() => {
    setTradeFocus(null)
    window.history.replaceState(null, '', '#trade')
  }, [])
  // In-page links (<a href="#live">) switch tabs too.
  useEffect(() => {
    const onHash = () => {
      const parts = window.location.hash.slice(1).split('/')
      const h = parts[0] as Tab
      if (TABS.includes(h)) setTabState(h)
      if (h === 'trade') setTradeFocus(focusFromHash())
      if (h === 'pricing') setWantFeature(parts[1] || null)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [executing, setExecuting] = useState(false)
  const [execMessage, setExecMessage] = useState<string | null>(null)
  const [tradingSymbol, setTradingSymbol] = useState<string | null>(null)
  // Which systems are trading real orders — shown as a red badge in the header on every tab.
  const [liveSystems, setLiveSystems] = useState<string[]>([])
  const refreshLive = useCallback(async () => {
    try {
      const s = await api.live.status()
      setLiveSystems(Object.entries(s.systems).filter(([, v]) => v.live).map(([k]) => k))
    } catch {
      // the badge is informational — the Live tab shows errors
    }
  }, [])
  useEffect(() => {
    refreshLive()
    const id = setInterval(refreshLive, 30_000)
    return () => clearInterval(id)
  }, [refreshLive])

  // Only runs once the Signal tab has been opened — see the note in useOracle.
  const [signalTabOpened, setSignalTabOpened] = useState(tab === 'signal')
  const { data: oracle, loading, error, refresh, lastUpdated, countdown } =
    useOracle(symbol, signalTabOpened)

  const { isAlarming, dismiss, autoTradeMsg } = useAlarm(
    oracle?.oracle ?? null,
    oracle?.signal_package ?? null,
    symbol,
    () => setTab('trades'),
  )

  async function handleExecute() {
    setExecuting(true)
    setExecMessage(null)
    try {
      const result = await api.oracle.executeAndTrade(symbol)
      setExecMessage(result.message)
      if (result.trade) setTab('trades')
    } catch (e) {
      setExecMessage(e instanceof Error ? e.message : 'Execution failed')
    } finally {
      setExecuting(false)
    }
  }

  const openSignalTab = useCallback((next: string) => {
    setSymbol(next)
    setSignalTabOpened(true)
    setTab('signal')
  }, [])

  const handleSignalTrade = useCallback(async (tradeSymbol: string) => {
    setTradingSymbol(tradeSymbol)
    setExecMessage(null)
    try {
      const result = await api.oracle.executeAndTrade(tradeSymbol)
      setExecMessage(result.message)
      if (result.trade) setTab('trades')
    } catch (e) {
      setExecMessage(e instanceof Error ? e.message : 'Execution failed')
    } finally {
      setTradingSymbol(null)
    }
  }, [setTab])

  const rec = oracle?.oracle?.trade_recommendation
  const canTrade = rec && rec.action !== 'HOLD' && rec.confidence >= 60

  return (
    <div className={clsx('min-h-screen bg-oracle-bg text-gray-100', isAlarming && 'pt-14')}>
      {/* Alarm banner — fixed at top, pushes layout down via pt-14 */}
      {isAlarming && oracle && (
        <AlarmBanner
          oracle={oracle.oracle}
          signal={oracle.signal_package}
          onDismiss={dismiss}
        />
      )}

      <DataWatchBanner />

      {/* Header */}
      <header className={clsx(
        'border-b border-gray-800 py-3 z-10 bg-oracle-bg/95 backdrop-blur',
        isAlarming ? 'sticky top-14' : 'sticky top-0',
      )}>
        <div className={clsx('mx-auto px-4 sm:px-6 flex items-center justify-between',
          'max-w-none')}>
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center">
              <Zap className="w-5 h-5 text-white" />
            </div>
            <div>
              <div className="font-black text-white text-lg leading-none">Crypto Oracle</div>
              <div className="hidden sm:block text-xs text-gray-500">AI-Powered Trading Signals</div>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <HeaderClock />
            <MarketStateChip />
            <CalendarChip />
            {liveSystems.length > 0 && (
              <button onClick={() => setTab('live')}
                className="text-[11px] font-black tracking-wider bg-rose-600 hover:bg-rose-500 text-white px-2 py-1 rounded animate-pulse whitespace-nowrap"
                title={`Real orders are being sent to Binance: ${liveSystems.join(', ')}`}>
                LIVE<span className="hidden sm:inline"> · {liveSystems.join(', ')}</span><span className="sm:hidden"> · {liveSystems.length}</span>
              </button>
            )}
            {/* Live price */}
            {oracle?.signal_package?.price && (
              <div className="text-right hidden sm:block">
                <div className="text-white font-bold text-sm">
                  ${oracle.signal_package.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                </div>
                <div className="text-xs text-gray-500">{symbol.replace('USDT', '/USDT')}</div>
              </div>
            )}

            {/* Refresh countdown */}
            {lastUpdated && (
              <div className="flex items-center gap-1.5 text-xs text-gray-500">
                <Clock className="w-3 h-3" />
                <span>{countdown}s</span>
              </div>
            )}

            {/* Live pulse */}
            {loading && (
              <div className="flex items-center gap-1.5 text-xs text-blue-400">
                <Activity className="w-3 h-3 animate-pulse" />
                <span>Analyzing...</span>
              </div>
            )}

            {/* Cached badge */}
            {oracle?.cached && !loading && (
              <span className="text-xs bg-gray-800 text-gray-500 px-2 py-0.5 rounded">cached</span>
            )}

            {/* Account: who is signed in, their plan; Plans & billing, Settings, Sign out */}
            <AccountMenu onOpenPricing={() => openPricing()} onOpenSettings={() => setSettingsOpen(true)} />
          </div>
        </div>
      </header>

      {/* Full browser width — the trading screens need the room; px keeps content off the edges. */}
      <div className={clsx('mx-auto px-3 sm:px-5 2xl:px-8 py-5 space-y-5', 'max-w-none')}>
        {/* Symbol selector + controls — only relevant to the per-symbol Signal tab */}
        <div className={clsx('flex flex-wrap items-center gap-2', tab !== 'signal' && 'hidden')}>
          <div className="flex gap-1.5 flex-wrap">
            {SYMBOLS.map(s => (
              <button
                key={s}
                onClick={() => setSymbol(s)}
                className={clsx(
                  'px-3 py-1.5 rounded-lg text-sm font-semibold transition-all',
                  symbol === s ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white',
                )}
              >
                {s.replace('USDT', '')}
              </button>
            ))}
          </div>

          <div className="ml-auto flex gap-2">
            <button
              onClick={refresh}
              disabled={loading}
              className="flex items-center gap-2 bg-gray-800 hover:bg-gray-700 text-white px-3 py-1.5 rounded-lg text-sm font-semibold transition-all disabled:opacity-50"
            >
              <RefreshCw className={clsx('w-4 h-4', loading && 'animate-spin')} />
              Refresh
            </button>

            {canTrade && (
              <button
                onClick={handleExecute}
                disabled={executing}
                className={clsx(
                  'flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-bold transition-all disabled:opacity-50',
                  rec?.action === 'BUY' ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-red-600 hover:bg-red-500',
                  'text-white',
                )}
              >
                <Zap className="w-4 h-4" />
                {executing ? 'Opening...' : `Paper ${rec?.action}`}
              </button>
            )}
          </div>
        </div>

        {/* Exec / auto-trade messages */}
        {(execMessage || autoTradeMsg) && (
          <div className="space-y-1">
            {execMessage && (
              <div className={clsx(
                'text-sm px-4 py-2 rounded-lg border',
                execMessage.includes('opened')
                  ? 'bg-emerald-900/30 border-emerald-800 text-emerald-400'
                  : 'bg-gray-800 border-gray-700 text-gray-400',
              )}>
                {execMessage}
              </div>
            )}
            {autoTradeMsg && (
              <div className={clsx(
                'text-sm px-4 py-2 rounded-lg border',
                autoTradeMsg.includes('opened')
                  ? 'bg-blue-900/30 border-blue-800 text-blue-400'
                  : 'bg-gray-800 border-gray-700 text-gray-400',
              )}>
                ⚡ Auto-trade: {autoTradeMsg}
              </div>
            )}
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b border-gray-800 overflow-x-auto">
          {([
            ['all', Radar, 'All Signals'],
            ['signal', BarChart2, 'Signal'],
            ['news', Newspaper, 'News'],
            ['calendar', CalendarClock, 'Calendar'],
            ['trade', CandlestickChart, 'Trade'],
            ['trades', List, 'Trades'],
            ['history', History, 'History'],
            ['markets', Globe2, 'Markets'],
            ['copy', Users, 'Copy'],
            ['mentor', Crosshair, 'Mentor'],
            ['prop', Trophy, 'Prop'],
            ['one', Layers, 'One book'],
            ['control', FlaskConical, 'Control'],
            ['lab', Microscope, 'Lab'],
            ['retired', Archive, 'Retired'],
            ['live', ShieldAlert, 'Live'],
            ['books', Layers, 'Books'],
            ['portfolio', Briefcase, 'Portfolio'],
            ['pricing', CreditCard, 'Plans'],
            ['admin', ShieldCheck, 'Admin'],
          ] as const).filter(([t]) => t !== 'admin' || isAdmin).map(([t, Icon, label]) => {
            const lock = locked(t)
            return (
              <button
                key={t}
                onClick={() => {
                  if (lock) { openPricing(lock); return }
                  if (t === 'signal') setSignalTabOpened(true)
                  setTab(t)
                }}
                title={lock ? 'Not in your plan — see the plans' : undefined}
                aria-disabled={lock ? true : undefined}
                className={clsx(
                  'flex items-center gap-2 pb-3 px-1 mr-3 text-sm font-semibold border-b-2 transition-all whitespace-nowrap shrink-0',
                  tab === t ? 'border-blue-500 text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-300',
                  lock && 'text-gray-600 hover:text-gray-500',
                )}
              >
                <Icon className="w-4 h-4" />
                {label}
                {lock && <Lock className="w-3 h-3 text-gray-600" aria-label="locked" />}
              </button>
            )
          })}
        </div>

        {/* Free plan: the signals are there, 15 minutes late */}
        {delayedSignals && (
          <div className="flex flex-wrap items-center gap-2 bg-amber-900/30 border border-amber-800 rounded-lg px-4 py-2 text-amber-300 text-xs">
            <Clock className="w-3.5 h-3.5 shrink-0" />
            <span>Signals are delayed {session?.limits.signal_delay_min || 15} minutes on the Free plan — <button onClick={() => openPricing('signals.live')} className="underline font-semibold hover:text-amber-200">upgrade for live signals</button>.</span>
          </div>
        )}

        {/* A locked tab reached by hash (bookmark, sign-out and back) goes to the plans */}
        {locked(tab) && tab !== 'pricing' && (
          <div className="card text-sm text-gray-400 flex flex-wrap items-center gap-2">
            <Lock className="w-4 h-4 text-gray-500" /> This tab is not part of your plan.
            <button onClick={() => openPricing(locked(tab))} className="text-blue-300 hover:underline">See the plans</button>
          </div>
        )}

        {/* Error */}
        {tab === 'signal' && error && (
          <div className="flex items-center gap-2 bg-red-900/30 border border-red-800 rounded-lg px-4 py-3 text-red-400 text-sm">
            <AlertCircle className="w-4 h-4 shrink-0" />
            {error}
          </div>
        )}

        {/* All Signals tab */}
        {tab === 'all' && !locked('all') && (
          <AllSignals
            onSelectSymbol={openSignalTab}
            onTrade={handleSignalTrade}
            trading={tradingSymbol}
          />
        )}

        {/* Loading skeleton */}
        {tab === 'signal' && loading && !oracle && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {[1, 2].map(i => (
              <div key={i} className="card animate-pulse h-64">
                <div className="h-5 bg-gray-800 rounded w-1/3 mb-4" />
                <div className="space-y-3">
                  <div className="h-16 bg-gray-800 rounded" />
                  <div className="h-8 bg-gray-800 rounded" />
                  <div className="h-8 bg-gray-800 rounded" />
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Signal tab */}
        {tab === 'signal' && oracle && !locked('signal') && (
          <div className="space-y-4">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <OracleSignal oracle={oracle.oracle} lastUpdated={lastUpdated} />
              <SignalScores signal={oracle.signal_package} />
            </div>
            <PriceChart
              symbol={symbol}
              recommendation={oracle.oracle?.trade_recommendation}
              timeframeSignals={oracle.signal_package.ta_summary.timeframe_signals}
            />
            {oracle.pipeline_status && (
              <PipelineStatus status={oracle.pipeline_status} />
            )}
          </div>
        )}

        <Suspense fallback={<div className="card text-sm text-gray-500">Loading…</div>}>
          {/* Plans (everyone) and the admin console (role admin) */}
          {tab === 'pricing' && <PricingPage highlight={wantFeature} />}
          {tab === 'admin' && (isAdmin ? <AdminPage /> : <div className="card text-sm text-gray-500">The admin console is for administrators.</div>)}

          {!locked(tab) && <>
          {/* Binance-style terminal: your own orders, paper or live (main wallet) */}
          {tab === 'news' && <NewsPage />}
          {tab === 'calendar' && <CalendarPage />}
          {tab === 'trade' && <TradeTerminal focus={tradeFocus} onClearFocus={clearFocus} />}

          {/* Portfolio tab */}
          {tab === 'portfolio' && <Portfolio onOpenTrades={() => setTab('trades')} />}

          {/* Every trade from every system */}
          {tab === 'trades' && <TradesPage onChart={openOnChart} />}

          {/* Trade history + backtest — both wallets */}
          {tab === 'history' && <TradeHistory />}

          {/* Side experiments: what does Claude add, and can it trade on its own? */}
          {tab === 'lab' && <LabPage />}

          {/* Everything we tried that failed, with the evidence */}
          {tab === 'markets' && <MarketsPage />}
          {tab === 'copy' && <CopyPage />}
          {tab === 'mentor' && <AuctionPage />}
          {tab === 'prop' && <PropPage />}
          {tab === 'one' && <CompositePage />}
          {tab === 'control' && <ControlPage />}
          {tab === 'books' && <BooksPage />}
          {tab === 'retired' && <RetiredPage />}

          {/* Paper/live switch per system, Binance account, kill switch, audit */}
          {tab === 'live' && <LivePage onChange={refreshLive} />}
          </>}

          {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} onOpenPricing={f => { setSettingsOpen(false); openPricing(f) }} />}
        </Suspense>
      </div>
    </div>
  )
}
