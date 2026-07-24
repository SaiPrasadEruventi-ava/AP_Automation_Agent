# Deploying to Vercel with Redis

This app has been restructured to run as a Vercel serverless Python
function, with Redis (Upstash) replacing the three things that used
to live on local disk:

| Old (local disk)                              | New (Redis)                                  |
|------------------------------------------------|-----------------------------------------------|
| `data/erp/ERP_Mock_Data.xlsx`                   | PO/GRN hashes (`erp_mock.py`)                 |
| `data/pending_reviews/{token}.json`             | Pending-review strings + index set            |
| `data/processed/processed_invoices.json`        | Processed-invoice hash + date-sorted set      |
| `gmail_token.json`                              | Gmail OAuth refresh token (`gmail_auth.py`)   |

**I can't run the actual deployment for you** — I have no Vercel
account access or credentials. Everything below is for you to run
yourself, either via the Vercel CLI or the Vercel dashboard + GitHub.

## 1. Push this code to a GitHub repo

Vercel deploys from a git repo (or via `vercel --prod` from the CLI
directly). Either works.

## 2. Create the Vercel project and add Upstash Redis

1. In the Vercel dashboard: **Add New → Project**, import the repo.
2. In the project's **Storage** tab: **Create Database → Upstash →
   Redis** (or **Marketplace → Upstash**). Link it to this project —
   Vercel will automatically set `UPSTASH_REDIS_REST_URL` and
   `UPSTASH_REDIS_REST_TOKEN` as environment variables for you.

## 3. Set the remaining environment variables

In **Project Settings → Environment Variables**, add everything from
`.env.example` that applies to your setup (Mistral key, mail provider
choice + Graph or Gmail credentials, notify/reviewer emails, variance
threshold, approval portal URL, etc.) — same variables as running
locally, just entered in Vercel's UI instead of a `.env` file.

Also set:
```
AP_TRIGGER_SECRET=<generate with: python -c "import secrets; print(secrets.token_urlsafe(32))">
AP_APPROVAL_PORTAL_URL=https://<your-project>.vercel.app
```

**Gmail users only:** OAuth's interactive consent flow needs a real
browser, which Vercel doesn't have. Run the one-time consent flow
**locally** first (`python run_ap.py --test` with `AP_GMAIL_AUTH_MODE=oauth`
will trigger it), which now caches the resulting token to Redis via
`gmail_auth.py`. As long as that local run used the *same* Upstash
Redis instance (point your local `.env` at it temporarily), Vercel
will find the cached token there — no browser needed at runtime.
Service-account mode doesn't have this problem (no interactive step),
but its JSON key must be provided as an env var since there's no file
to bundle — see `AP_GMAIL_SERVICE_ACCOUNT_FILE` and adapt
`gmail_auth.py` if you go this route (it currently expects a file path;
you'd base64-decode an env var to a temp file, or extend
`redis_store.py` the same way the OAuth token was handled).

## 4. Migrate the ERP mock data into Redis (one time, locally)

```bash
pip install -r requirements-local.txt
python migrate_erp_to_redis.py
```

Point your local `.env`'s `UPSTASH_REDIS_REST_URL` /
`UPSTASH_REDIS_REST_TOKEN` at the *same* Redis instance the Vercel
project uses (copy them from Vercel's env var settings) before running
this, so the data lands where the deployed app will look for it.

## 5. Deploy

```bash
npm i -g vercel      # if you don't already have the CLI
vercel login
vercel --prod
```

Or just push to the branch Vercel is watching if you connected via
GitHub — it deploys automatically on push.

## 6. Verify

```bash
curl https://<your-app>.vercel.app/health
curl https://<your-app>.vercel.app/pending-reviews
```

## 7. Triggering a processing run

Polling was intentionally **not** wired to Vercel Cron (per your
choice) — call this yourself whenever you want a pass over the mailbox:

```bash
curl -X POST https://<your-app>.vercel.app/api/process \
     -H "X-Trigger-Secret: <the secret you set in AP_TRIGGER_SECRET>"
```

This runs the same logic as one un-watched `python run_ap.py` pass:
drains everything currently in the `ToBeProcessed` folder, sends
review/result emails, and returns a JSON summary. If you later want
this on a schedule, Vercel Cron Jobs (`vercel.json`'s `crons` field)
can hit this same endpoint on an interval — ask and I can wire that up.

## Known limitations of this architecture on Vercel

- **Execution time limits.** Vercel functions time out (10s on Hobby,
  up to 300s on Pro/Enterprise with configuration). If a batch of
  invoices is large or Mistral/Graph/Gmail calls are slow, a single
  `/api/process` call could hit that ceiling mid-batch. Consider
  capping how many emails one call drains, or splitting into multiple
  calls, if you hit this in practice.
- **No `--watch` mode.** By design (per your choice) — you decide when
  processing runs, via your own external trigger.
- **Cold starts.** Each serverless invocation may re-authenticate with
  Graph/Gmail/Mistral from scratch (in-memory token caches don't
  survive between invocations, only the Redis-backed ones do).
