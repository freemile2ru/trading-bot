# Deploying the app

One instance in AWS Tokyo (`ap-northeast-1`) — Binance's API refuses US addresses, and Tokyo is where its matching
engine runs. The app is a single stateful process over SQLite files, so it runs as one Docker Compose project on one
box with its data on its own EBS volume. The dashboard is served by Caddy on your domain with TLS and one password.

```
GitHub (main) ──deploy.yml──▶ GHCR images ──┐
                                            ▼  SSM Run Command (no SSH)
                      EC2 t4g.medium, Tokyo: caddy :443 ─▶ backend :8000 ─▶ redis
                                            └ /srv/ai-trader/data (EBS)  ──nightly──▶ S3 backups
```

## One-time setup

1. **AWS account, your credentials on this machine** (`aws sts get-caller-identity` works), Terraform ≥ 1.6, and the
   [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
   for a shell on the box.
2. **State backend**: `infra/bootstrap.sh` — creates the state bucket and lock table and prints the repository variables.
3. **First apply from your machine** (the GitHub roles don't exist yet):
   ```
   cd infra/terraform
   terraform init -backend-config="bucket=…" -backend-config="dynamodb_table=…" -backend-config="region=ap-northeast-1"
   terraform apply -var domain=trader.example.com            # add -var route53_zone_id=Z… if the zone is in Route 53
   ```
   Note the outputs. If DNS is elsewhere, create an **A record** for the domain → `elastic_ip`.
4. **Secrets** into SSM Parameter Store (never into git or GitHub):
   ```
   aws ssm put-parameter --region ap-northeast-1 --overwrite --type SecureString --name /ai-trader/app-env --value "$(cat backend/.env)"
   HASH=$(docker run --rm caddy:2-alpine caddy hash-password --plaintext 'YOUR-PASSWORD')
   aws ssm put-parameter --region ap-northeast-1 --overwrite --type SecureString --name /ai-trader/web-env \
       --value "DOMAIN=trader.example.com
   BASIC_AUTH_USER=you
   BASIC_AUTH_HASH=$HASH"
   aws ssm put-parameter --region ap-northeast-1 --overwrite --type SecureString --name /ai-trader/ghcr-user  --value "freemile2ru"
   aws ssm put-parameter --region ap-northeast-1 --overwrite --type SecureString --name /ai-trader/ghcr-token --value "ghp_…"   # a classic PAT with read:packages
   ```
   In `app-env`, drop `HOST`/`PORT`/`REDIS_URL` (compose sets them) and keep `BINANCE_ENV` on the demo account.
5. **GitHub → Settings → Secrets and variables → Actions → Variables**:
   `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `AWS_TERRAFORM_ROLE_ARN`, `AWS_INSTANCE_ID`, `RELEASE_BUCKET`,
   `TF_STATE_BUCKET`, `TF_LOCK_TABLE`, `APP_DOMAIN`, optionally `ROUTE53_ZONE_ID`.
   **Environments**: create `production` and `infra`; add yourself as a required reviewer on each if you want every
   deploy / apply to wait for a click.
6. **Check Binance answers from Tokyo** before trusting anything: `aws ssm start-session --target <instance id>`,
   then `cat /var/log/ai-trader-binance-check.log` — every URL should show `200`.
7. **Push to `main`** → Deploy runs: tests → images → `deploy.sh` on the box → health check. First deploy starts the app empty.
8. **Move the data** (one-off, from the Mac, while the Mac's backend is stopped):
   ```
   tar -C backend -czf /tmp/data.tgz --exclude='*.db-wal' --exclude='*.db-shm' --exclude='*.py' --exclude=__pycache__ data
   aws s3 cp /tmp/data.tgz s3://<RELEASE_BUCKET>/release/manual/data.tgz
   # on the box (session manager):
   cd /srv/ai-trader && docker compose stop backend && aws s3 cp s3://<RELEASE_BUCKET>/release/manual/data.tgz /tmp/ \
     && tar -xzf /tmp/data.tgz -C /srv/ai-trader --strip-components=1 && chown -R 1000:1000 data && docker compose start backend
   ```
   Stop the Mac's `./start.sh` for good at this point — two copies would trade the same account.

## The database

Production runs on **RDS PostgreSQL 16** (`db.t4g.micro`, private to the VPC, 7-day automated backups, deletion
protection). `storage/engine.py` is the switch: with `DATABASE_URL` set (deploy.sh adds it to `app.env` from SSM)
every database the app opens through `storage.db.connect()` lives in Postgres, one schema per former SQLite file;
without it the app runs on SQLite files exactly as in development. The test suite runs on both (`TEST_DATABASE_URL`).

The migration is staged. Already on Postgres when `DATABASE_URL` is set: the kv state, every paper and live wallet,
signals and scan labels, the pair books (carry, carry-q, events, unlocks, vol premium), the news store. Still on
SQLite files (next stages, book by book): the copy-trading observation log, cascade, positioning, funding venues,
the unlock snapshots, the research labs' read-only replays.

**Moving the data** (one-off, after the first deploy, with the Mac's backend stopped — see step 8 for the files):
```
# on the box, with the data files in /srv/ai-trader/data:
docker compose run --rm backend python scripts/sqlite_to_postgres.py --all
```
The script creates each schema from the SQLite DDL and copies rows in batches; it can be re-run to top up. The SQLite
files stay on the volume for the books that haven't moved and as the fallback (`unset DATABASE_URL`).

## Day to day

- **Deploy**: merge to `main`. Rollback: re-run the Deploy workflow from the previous commit (`workflow_dispatch`),
  or on the box `./deploy.sh <previous backend tag> <previous web tag>`.
- **Infra change**: PR touching `infra/` gets a plan comment; merging applies.
- **Logs**: `docker compose logs -f backend` on the box (JSON files, 5 × 50 MB kept).
- **Backups**: `s3://<bucket>/backups/<date>/sqlite-HHMM.tar.zst`, 30-day retention; restore = extract into `data/` with the backend stopped.
- **Secrets change**: `put-parameter --overwrite` then re-run Deploy (deploy.sh re-reads SSM every time).
- **Bigger box**: change `instance_type` in a PR; the data volume and elastic IP survive the resize.

## What is deliberately not here

No autoscaling, load balancer, container orchestrator or managed database: the app's schedulers and SQLite files
require exactly one writer, and a second instance would double-trade the account. Local development is unchanged
(`./start.sh`); `APP_ENV=production` is what switches off uvicorn's reloader inside the container.
