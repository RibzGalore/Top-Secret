# Community Bank Thrift Conversion Monitor

Monitors thezenofthriftconversions.com for new IPOs and thrift conversions.
When a new bank appears, automatically fetches the SEC prospectus, runs the
10-point community bank investing checklist via Claude, and emails you a
full analysis.

Also sends a weekly digest every Monday — even with no new additions, so you
always get a confirmation that the monitor ran.

---

## Setup (5 minutes)

### 1. Install dependencies

```bash
pip install requests beautifulsoup4 anthropic
```

### 2. Set environment variables

Create a `.env` file (or export these in your shell):

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...your key here...

# Email — Gmail example
EMAIL_FROM=you@gmail.com
EMAIL_TO=you@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your_app_password   # Use a Gmail App Password, not your main password
                               # Generate at: myaccount.google.com/apppasswords
```

For other email providers:
- Outlook/Hotmail: SMTP_HOST=smtp-mail.outlook.com, SMTP_PORT=587
- Yahoo: SMTP_HOST=smtp.mail.yahoo.com, SMTP_PORT=587

### 3. Test run

```bash
export $(cat .env | xargs)
python thrift_monitor.py
```

On first run it creates `thrift_state.json` which tracks all banks seen so far.

---

## Scheduling

### Mac / Linux (cron) — runs every Monday at 8am

```bash
crontab -e
```
Add this line:
```
0 8 * * 1 cd /path/to/folder && /usr/bin/python3 thrift_monitor.py >> monitor.log 2>&1
```

### Windows (Task Scheduler)

1. Open Task Scheduler → Create Basic Task
2. Trigger: Weekly, Monday, 8:00 AM
3. Action: Start a program
   - Program: `python`
   - Arguments: `C:\path\to\thrift_monitor.py`
   - Start in: `C:\path\to\folder`

---

## GitHub Actions (free, runs in the cloud — no local machine needed)

Create `.github/workflows/thrift_monitor.yml` in a private GitHub repo:

```yaml
name: Thrift Conversion Monitor

on:
  schedule:
    - cron: '0 8 * * 1'   # Every Monday at 8am UTC
  workflow_dispatch:        # Also allows manual trigger from GitHub UI

jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install requests beautifulsoup4 anthropic

      - name: Restore state
        uses: actions/cache@v4
        with:
          path: thrift_state.json
          key: thrift-state-${{ github.run_id }}
          restore-keys: thrift-state-

      - name: Run monitor
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          EMAIL_FROM:        ${{ secrets.EMAIL_FROM }}
          EMAIL_TO:          ${{ secrets.EMAIL_TO }}
          SMTP_HOST:         ${{ secrets.SMTP_HOST }}
          SMTP_PORT:         ${{ secrets.SMTP_PORT }}
          SMTP_USER:         ${{ secrets.SMTP_USER }}
          SMTP_PASS:         ${{ secrets.SMTP_PASS }}
        run: python thrift_monitor.py

      - name: Save state
        uses: actions/cache@v4
        with:
          path: thrift_state.json
          key: thrift-state-${{ github.run_id }}
```

Add your secrets at: GitHub repo → Settings → Secrets and variables → Actions

This is the best option if you don't want to leave a machine running 24/7.
It's free for public repos and has 2,000 minutes/month free for private repos
(this script uses about 1 minute per run).

---

## What the email looks like

**When no new banks are detected:**
> Subject: [Thrift Monitor] No new additions this week — Apr 21
>
> ✓ No new thrift conversions this week. Your watchlist is unchanged.

**When a new bank is detected:**
> Subject: [Thrift Monitor] 1 new bank detected — Apr 21
>
> [Full 10-point checklist scorecard with score, verdict, analyst take,
>  metrics table, red flags, green flags, and buy/wait/avoid recommendation]

---

## Files

| File | Purpose |
|------|---------|
| `thrift_monitor.py` | Main script |
| `thrift_state.json` | Auto-created; tracks all banks seen so far |
| `.env` | Your secrets (never commit this to git) |

---

## Notes

- The thezenofthriftconversions.com table is JavaScript-rendered (Wix), so the
  scraper also cross-checks SEC EDGAR directly for recent thrift S-1 filings —
  this is actually more reliable than scraping the site itself.

- The script costs roughly $0.01–0.05 per new bank analyzed via the Claude API.
  Weekly no-new-addition runs cost nothing (no API call made).

- `thrift_state.json` is your memory — don't delete it or the script will
  re-analyze banks it has already seen.
