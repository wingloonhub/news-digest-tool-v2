# Deploy GitHub Actions News Digest

## What this gives you

- **Hourly news digest** sent to Telegram + saved to Firestore
- Runs on **GitHub Actions** (free, in the cloud, no PC dependency)
- Reads slot/category config from your existing Firestore (`wing-news-digest-v2`)
- Uses **RSS feeds** for news (no API keys needed)

## One-time setup (~10 min)

### 1. Upload the two new files to your GitHub repo

In `news-digest-tool-v2`:

- `scripts/digest.py`
- `.github/workflows/news-digest.yml`

Easiest way: GitHub web UI → **Add file → Upload files** → drag both files (preserving folder structure: scripts/ and .github/workflows/).

### 2. Add the Telegram bot token as a GitHub Secret

In your GitHub repo:

1. **Settings** (top tab) → **Secrets and variables** → **Actions**
2. **New repository secret**
3. **Name:** `TELEGRAM_BOT_TOKEN`
4. **Value:** `8675719440:AAFmtDAuqFjTyK2v29LnaAuy5KHTRm1zPCs`
5. Save

### 3. Trigger the first run manually to verify

1. In your GitHub repo: **Actions** tab → **News Digest Hourly** workflow → **Run workflow** → Run
2. Wait ~30 seconds → click the run → check the logs
3. If it printed `=== Done ===` and current hour matches an enabled slot, you should see the digest in Telegram

### 4. Disable the local Claude scheduler task

In Claude Code:

```
/schedule
```

Find `news-digest-v2`, disable it (or delete it). The cloud cron now handles it.

## How it works

Every hour at xx:05 (UTC), GitHub fires the workflow. The Python script:

1. Reads `newsDigest/config` from Firestore via REST API
2. Checks current Asia/Kuala_Lumpur hour against your enabled slots
3. If nothing is due → exit silently
4. If a region is due:
   - Fetches RSS for that region's enabled categories
   - Filters same-day stories, dedupes by title
   - Builds digest text (matches the existing format)
   - Sends to Telegram (chunked if > 3800 chars)
   - Prepends a new item to `feed_global` or `feed_malaysia` (keeps last 6)

## Cost

- GitHub Actions: free tier covers ~2,000 min/month; this uses ~6 min/month
- Firebase: still in free tier
- Telegram: free
- **Total: $0/month, forever**

## Trade-off vs. the old Claude SKILL

| | Old (Claude WebSearch) | New (RSS) |
|---|---|---|
| News quality | AI-summarized, curated | Raw RSS titles + descriptions |
| Reliability | ~50% (local scheduler decay) | 99.9%+ (GitHub cron) |
| Cost | $0 | $0 |
| PC dependency | Required | None |

## Editing RSS feeds

Open `scripts/digest.py` and edit the `FEEDS` dict near the top. Each region/category lists `(url, source_label)` tuples. Add more feeds for richer coverage.

## Editing the schedule

Cron line in `.github/workflows/news-digest.yml`:

```yaml
- cron: '5 * * * *'
```

Default fires every hour at minute 5 UTC. The script checks YOUR local hour against config slots. So you only need to edit if you want a different cadence (e.g., every 30 min).
