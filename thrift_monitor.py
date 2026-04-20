#!/usr/bin/env python3
"""
Community Bank Thrift Conversion Monitor
=========================================
Monitors thezenofthriftconversions.com for new IPOs/conversions weekly.
When a new bank is found, fetches the SEC prospectus and runs the
10-point checklist analysis via Claude. Sends results by email.

Setup:
  pip install requests beautifulsoup4 anthropic

Required environment variables (add to .env or export before running):
  ANTHROPIC_API_KEY   - your Anthropic API key
  EMAIL_FROM          - sender email address
  EMAIL_TO            - your email address
  SMTP_HOST           - e.g. smtp.gmail.com
  SMTP_PORT           - e.g. 587
  SMTP_USER           - SMTP username (usually same as EMAIL_FROM)
  SMTP_PASS           - SMTP password or app password

Schedule (cron example — runs every Monday at 8am):
  0 8 * * 1 /usr/bin/python3 /path/to/thrift_monitor.py

Schedule (GitHub Actions — see README at bottom of file)
"""

import os
import json
import smtplib
import hashlib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import anthropic
import sendgrid
from sendgrid.helpers.mail import Mail

# ─── Configuration ────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EMAIL_FROM        = os.environ.get("EMAIL_FROM", "")
EMAIL_TO          = os.environ.get("EMAIL_TO", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_PASS         = os.environ.get("SMTP_PASS", "")
sg_key_check      = os.environ.get("SENDGRID_API_KEY", "")
print(f"DEBUG SendGrid key loaded: {bool(sg_key_check)}, length: {len(sg_key_check)}")
print(f"DEBUG EMAIL_FROM: '{EMAIL_FROM}', EMAIL_TO: '{EMAIL_TO}'")
TRACKER_URL       = "https://www.thezenofthriftconversions.com/thrift-conversions"
SEC_EDGAR_SEARCH  = "https://efts.sec.gov/LATEST/search-index?q=%22{name}%22&dateRange=custom&startdt={start}&enddt={end}&forms=S-1,424B3,10-K"
STATE_FILE        = Path("thrift_state.json")  # persists known banks between runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── The 10-point checklist prompt ────────────────────────────────────────────

CHECKLIST_SYSTEM = """You are a senior US community bank analyst with 15+ years of experience 
specializing in thrift conversions (mutual-to-stock IPOs). You analyze banks using this 
10-point checklist and return structured JSON only — no preamble, no markdown fences.

The checklist:
1. Assets under $50B (regional bank profile)
2. Efficiency ratio — target under 55%; excellent under 50%
3. Loan-to-deposit ratio — sweet spot 80-90%; below 80% = underdeployed; above 100% = risky
4. Net interest margin (NIM) — target 3%+ and stable/expanding
5. Loan composition — CRE concentration under 300% of capital
6. Non-performing loans — target under 1% of total loans; above 2% = red flag
7. Tangible book value — don't pay more than 1.5x TBV unless ROE >15%
8. Deposit structure — sticky low-cost deposits (checking) vs expensive CDs
9. Management quality — do they take responsibility or make excuses?
10. Capital ratios — CET1/Tier1 above 10% is solid; below 8% is constrained

Return ONLY valid JSON in this exact schema:
{
  "bank_name": "string",
  "ticker": "string or null",
  "ipo_date": "string or null",
  "offer_price": "number or null",
  "total_assets_m": "number or null (millions USD)",
  "score": "integer 0-10",
  "verdict": "string (2-3 sentence summary)",
  "analyst_take": "string (4-6 sentence detailed analyst perspective)",
  "checklist": [
    {
      "number": 1,
      "title": "string",
      "result": "PASS | FAIL | CAUTION | INSUFFICIENT_DATA",
      "metric": "string (the actual number/ratio found)",
      "detail": "string (1-2 sentence explanation)"
    }
    // ... items 2-10
  ],
  "red_flags": ["string"],
  "green_flags": ["string"],
  "recommendation": "BUY_WATCH | WAIT_FOR_FILINGS | AVOID | INSUFFICIENT_DATA"
}"""

CHECKLIST_PROMPT = """Analyze this thrift conversion bank based on the prospectus/filing text below.
Apply the 10-point checklist and return the JSON scorecard.

Bank: {bank_name}
Source: {source_url}

--- FILING CONTENT ---
{filing_text}
--- END FILING CONTENT ---

Return ONLY the JSON object. No markdown, no explanation outside the JSON."""

# ─── State management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load previously seen banks from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"banks": {}, "last_run": None}


def save_state(state: dict):
    """Persist state to disk."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Web scraping ─────────────────────────────────────────────────────────────

def fetch_thrift_list() -> list[dict]:
    """
    Scrape thezenofthriftconversions.com for the current bank list.
    
    The site is Wix-rendered so the table doesn't appear in raw HTML.
    We use a realistic browser User-Agent and look for any visible text
    blocks, plus fall back to a secondary SEC EDGAR search for recent
    thrift S-1 filings as a cross-check.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }

    banks = []

    # Primary: try to get the Wix page content
    try:
        resp = requests.get(TRACKER_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Wix renders tables as divs; look for ticker-like text patterns
        text_blocks = soup.get_text(separator="\n")
        lines = [l.strip() for l in text_blocks.splitlines() if l.strip()]

        # Look for lines that look like bank entries (contains $, IPO, or known keywords)
        for i, line in enumerate(lines):
            if any(k in line for k in ["Savings Bank", "Bancorp", "Federal Savings", "MHC", "Thrift"]):
                banks.append({"name": line, "source": TRACKER_URL, "raw": line})

    except Exception as e:
        log.warning(f"Could not fetch tracker page: {e}")

    # Secondary: query SEC EDGAR full-text search for recent thrift S-1 filings
    # This is the most reliable signal for new conversions
    sec_banks = fetch_recent_sec_thrift_filings()
    
    # Merge, deduplicating by a hash of the bank name
    seen = {hashlib.md5(b["name"].encode()).hexdigest() for b in banks}
    for b in sec_banks:
        h = hashlib.md5(b["name"].encode()).hexdigest()
        if h not in seen:
            banks.append(b)
            seen.add(h)

    log.info(f"Found {len(banks)} banks in total scan")
    return banks


def fetch_recent_sec_thrift_filings() -> list[dict]:
    """
    Query SEC EDGAR for recent mutual-to-stock conversion S-1 and 424B3 filings.
    These are the definitive source of new thrift IPOs.
    """
    banks = []
    try:
        # Search for thrift conversion filings in the last 90 days
        url = (
            "https://efts.sec.gov/LATEST/search-index?q=%22mutual+to+stock%22"
            "+%22savings+bank%22&forms=S-1,424B3&dateRange=custom"
            f"&startdt=2026-01-01&enddt={datetime.now().strftime('%Y-%m-%d')}"
        )
        resp = requests.get(url, timeout=15, headers={"User-Agent": "thrift-monitor/1.0 contact@example.com"})
        if resp.ok:
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            for hit in hits[:20]:  # cap at 20
                src = hit.get("_source", {})
                entity = src.get("entity_name", "")
                form = src.get("form_type", "")
                filed = src.get("file_date", "")
                accession = src.get("accession_no", "")
                if entity:
                    banks.append({
                        "name": entity,
                        "form": form,
                        "filed": filed,
                        "accession": accession,
                        "source": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={requests.utils.quote(entity)}&type=S-1&dateb=&owner=include&count=10",
                        "raw": f"{entity} ({form}, {filed})"
                    })
    except Exception as e:
        log.warning(f"SEC EDGAR search failed: {e}")

    return banks


def fetch_prospectus_text(bank: dict) -> str:
    name = bank.get("name", "")
    
    # If a direct source URL to a filing is provided, fetch it directly
    source = bank.get("source", "")
    if source and "sec.gov/Archives" in source:
        try:
            resp = requests.get(source, timeout=20, headers={"User-Agent": "thrift-monitor/1.0 contact@example.com"})
            if resp.ok:
                log.info(f"Fetched filing directly for {name}: {len(resp.text)} chars")
                return resp.text[:15000]
        except Exception as e:
            log.warning(f"Direct fetch failed for {name}: {e}")

    # Try SEC EDGAR full-text search by company name
    try:
        search_url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{requests.utils.quote(name)}%22"
            f"&forms=S-1,424B3,10-K,10-Q&dateRange=custom&startdt=2024-01-01"
            f"&enddt={datetime.now().strftime('%Y-%m-%d')}"
        )
        resp = requests.get(search_url, timeout=15, headers={"User-Agent": "thrift-monitor/1.0 contact@example.com"})
        if resp.ok:
            hits = resp.json().get("hits", {}).get("hits", [])
            if hits:
                src = hits[0].get("_source", {})
                accession_no = src.get("accession_no", "")
                cik = src.get("entity_id", "")
                if accession_no and cik:
                    # Build direct filing URL
                    acc_clean = accession_no.replace("-", "")
                    filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{accession_no}-index.htm"
                    filing_resp = requests.get(filing_url, timeout=15, headers={"User-Agent": "thrift-monitor/1.0 contact@example.com"})
                    if filing_resp.ok:
                        # Parse index to find the main document
                        soup = BeautifulSoup(filing_resp.text, "html.parser")
                        for link in soup.find_all("a", href=True):
                            href = link["href"]
                            if any(ext in href.lower() for ext in [".htm", ".html"]) and "index" not in href.lower():
                                doc_url = f"https://www.sec.gov{href}" if href.startswith("/") else href
                                doc_resp = requests.get(doc_url, timeout=20, headers={"User-Agent": "thrift-monitor/1.0 contact@example.com"})
                                if doc_resp.ok and len(doc_resp.text) > 5000:
                                    log.info(f"Fetched SEC filing for {name}: {len(doc_resp.text)} chars")
                                    return doc_resp.text[:15000]
    except Exception as e:
        log.warning(f"SEC EDGAR search failed for {name}: {e}")

    log.warning(f"Could not fetch prospectus for {name} — returning placeholder")
    return f"[Prospectus text unavailable for {name}. Analysis based on bank name and public information only.]"

# ─── Email formatting ──────────────────────────────────────────────────────────

RESULT_COLORS = {
    "PASS": "#1D9E75",
    "FAIL": "#E24B4A",
    "CAUTION": "#BA7517",
    "INSUFFICIENT_DATA": "#888780"
}

RECOMMENDATION_COLORS = {
    "BUY_WATCH":          "#1D9E75",
    "WAIT_FOR_FILINGS":   "#BA7517",
    "AVOID":              "#E24B4A",
    "INSUFFICIENT_DATA":  "#888780"
}


def build_checklist_html(analysis: dict) -> str:
    """Render one bank's checklist analysis as an HTML block."""
    score = analysis.get("score", 0)
    rec = analysis.get("recommendation", "INSUFFICIENT_DATA")
    rec_color = RECOMMENDATION_COLORS.get(rec, "#888780")

    rows = ""
    for item in analysis.get("checklist", []):
        result = item.get("result", "INSUFFICIENT_DATA")
        color = RESULT_COLORS.get(result, "#888780")
        icon = {"PASS": "✓", "FAIL": "✗", "CAUTION": "⚠", "INSUFFICIENT_DATA": "?"}.get(result, "?")
        rows += f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;width:28px;">
            <span style="display:inline-block;width:22px;height:22px;border-radius:50%;
              background:{color}22;color:{color};text-align:center;line-height:22px;font-size:11px;font-weight:600;">{icon}</span>
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:13px;font-weight:600;color:#1a1a1a;">
            {item.get('number', '')}. {item.get('title', '')}
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:12px;color:#555;font-style:italic;">
            {item.get('metric', '—')}
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:12px;color:#555;">
            {item.get('detail', '')}
          </td>
        </tr>"""

    flags_html = ""
    red = analysis.get("red_flags", [])
    green = analysis.get("green_flags", [])
    if red or green:
        flags_html = "<div style='margin-top:16px;display:flex;gap:20px;flex-wrap:wrap;'>"
        if green:
            flags_html += "<div style='flex:1;min-width:200px;'><div style='font-size:11px;font-weight:600;color:#1D9E75;margin-bottom:6px;text-transform:uppercase;'>Green flags</div>"
            for f in green:
                flags_html += f"<div style='font-size:12px;color:#333;margin-bottom:4px;'>✓ {f}</div>"
            flags_html += "</div>"
        if red:
            flags_html += "<div style='flex:1;min-width:200px;'><div style='font-size:11px;font-weight:600;color:#E24B4A;margin-bottom:6px;text-transform:uppercase;'>Red flags</div>"
            for f in red:
                flags_html += f"<div style='font-size:12px;color:#333;margin-bottom:4px;'>✗ {f}</div>"
            flags_html += "</div>"
        flags_html += "</div>"

    return f"""
    <div style="background:#fff;border:1px solid #e8e8e8;border-radius:10px;padding:20px 24px;margin-bottom:24px;">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:10px;">
        <div>
          <div style="font-size:17px;font-weight:700;color:#1a1a1a;">{analysis.get('bank_name','Unknown')}</div>
          <div style="font-size:12px;color:#666;margin-top:2px;">
            {analysis.get('ticker') or 'Ticker pending'} &nbsp;·&nbsp;
            IPO {analysis.get('ipo_date') or 'TBD'} &nbsp;·&nbsp;
            Offer ${analysis.get('offer_price') or '—'}
            {(' &nbsp;·&nbsp; $' + str(analysis.get('total_assets_m')) + 'M assets') if analysis.get('total_assets_m') else ''}
          </div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:28px;font-weight:700;color:#1a1a1a;">{score}<span style="font-size:14px;color:#888;">/10</span></div>
          <div style="display:inline-block;margin-top:4px;padding:4px 12px;border-radius:6px;
            background:{rec_color}22;color:{rec_color};font-size:11px;font-weight:700;">
            {rec.replace('_', ' ')}
          </div>
        </div>
      </div>

      <div style="background:#f8f8f8;border-radius:8px;padding:12px 16px;margin-bottom:16px;
        font-size:13px;color:#333;line-height:1.6;border-left:3px solid {rec_color};">
        <strong>Verdict:</strong> {analysis.get('verdict','')}
      </div>

      <div style="background:#f0f8f4;border-radius:8px;padding:12px 16px;margin-bottom:16px;
        font-size:12px;color:#333;line-height:1.6;">
        {analysis.get('analyst_take','')}
      </div>

      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f5f5f5;">
            <th style="padding:6px 10px;text-align:left;font-size:11px;color:#666;font-weight:600;width:28px;"></th>
            <th style="padding:6px 10px;text-align:left;font-size:11px;color:#666;font-weight:600;">Criterion</th>
            <th style="padding:6px 10px;text-align:left;font-size:11px;color:#666;font-weight:600;">Metric</th>
            <th style="padding:6px 10px;text-align:left;font-size:11px;color:#666;font-weight:600;">Notes</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      {flags_html}
    </div>"""


def build_email_html(new_banks: list[dict], all_analyses: list[dict], no_new: bool) -> str:
    run_date = datetime.now().strftime("%B %d, %Y")

    if no_new:
        body_content = """
        <div style="background:#f0f8f4;border:1px solid #c8e6d8;border-radius:10px;
          padding:20px 24px;text-align:center;color:#0F6E56;">
          <div style="font-size:32px;margin-bottom:8px;">✓</div>
          <div style="font-size:16px;font-weight:600;">No new thrift conversions this week</div>
          <div style="font-size:13px;color:#555;margin-top:6px;">
            Your watchlist is unchanged. Check back next Monday.
          </div>
        </div>"""
    else:
        bank_blocks = "".join(build_checklist_html(a) for a in all_analyses)
        body_content = f"""
        <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;
          padding:12px 16px;margin-bottom:20px;font-size:13px;color:#856404;">
          <strong>{len(new_banks)} new addition{'s' if len(new_banks) > 1 else ''} detected</strong>
          — full checklist analysis below
        </div>
        {bank_blocks}"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:680px;margin:0 auto;padding:24px 16px;">

    <div style="margin-bottom:20px;">
      <div style="font-size:13px;font-weight:600;color:#1a1a1a;letter-spacing:0.05em;text-transform:uppercase;">
        Community Bank Investing Agent
      </div>
      <div style="font-size:11px;color:#888;margin-top:2px;">Weekly thrift conversion digest · {run_date}</div>
    </div>

    {body_content}

    <div style="margin-top:24px;padding-top:16px;border-top:1px solid #ddd;
      font-size:11px;color:#999;line-height:1.6;">
      Source: thezenofthriftconversions.com + SEC EDGAR · Analysis by Claude (Anthropic)<br>
      This is automated research output, not investment advice. Always verify figures against original filings.
    </div>
  </div>
</body></html>"""


# ─── Email sending ─────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str):
    if not all([EMAIL_FROM, EMAIL_TO]):
        log.warning("Email not configured — printing to stdout instead")
        print(f"\n{'='*60}\nSUBJECT: {subject}\n{'='*60}")
        return
    sg_key = os.environ.get("SENDGRID_API_KEY", "")
    if not sg_key:
        log.error("SENDGRID_API_KEY not set")
        return
    message = Mail(
        from_email=EMAIL_FROM,
        to_emails=EMAIL_TO,
        subject=subject,
        html_content=html_body
    )
    try:
        sg = sendgrid.SendGridAPIClient(api_key=sg_key)
        response = sg.send(message)
        log.info(f"Email sent via SendGrid — status {response.status_code}")
        log.info(f"SendGrid response body: {response.body}")
        log.info(f"SendGrid response headers: {response.headers}")
    except Exception as e:
        log.error(f"SendGrid error: {e}")
        log.error(f"SendGrid error type: {type(e)}")
        if hasattr(e, 'body'):
            log.error(f"SendGrid error body: {e.body}")
        raise

# ─── Main orchestration ────────────────────────────────────────────────────────

def main():
    log.info("=== Thrift Conversion Monitor starting ===")

    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable is required")

    state = load_state()
    known_banks = state.get("banks", {})

    # 1. Fetch current list of thrift conversions
    current_banks = fetch_thrift_list()

   # 2. Identify new additions (not seen in previous runs)
    new_banks = []
    for bank in current_banks:
        bank_id = hashlib.md5(bank["name"].encode()).hexdigest()
        if bank_id not in known_banks:
            new_banks.append(bank)
            known_banks[bank_id] = {"name": bank["name"], "first_seen": datetime.now().isoformat(), "source": bank.get("source", "")}

# TEST MODE — force Hoyne Bank through the pipeline
    new_banks.append({
        "name": "Hoyne Bancorp, Inc.",
        "ticker": "HYNE",
        "accession": "000110465925111398",
        "cik": "2073153",
        "source": "https://www.sec.gov/Archives/edgar/data/2073153/000110465925111398/hyne-20250930x10q.htm",
        "raw": "Hoyne Bancorp, Inc. (10-Q, 2025-11-13)"
    })

    log.info(f"New banks detected: {len(new_banks)}")

    # 3. For each new bank, fetch prospectus and run checklist
    analyses = []
    for bank in new_banks:
        log.info(f"Analyzing: {bank['name']}")
        filing_text = fetch_prospectus_text(bank)
        analysis = run_checklist_analysis(bank, filing_text)
        analyses.append(analysis)
        log.info(f"  Score: {analysis.get('score', '?')}/10 — {analysis.get('recommendation', '?')}")

    # 4. Build and send the weekly email
    no_new = len(new_banks) == 0
    subject = (
        f"[Thrift Monitor] No new additions this week — {datetime.now().strftime('%b %d')}"
        if no_new else
        f"[Thrift Monitor] {len(new_banks)} new bank{'s' if len(new_banks) > 1 else ''} detected — {datetime.now().strftime('%b %d')}"
    )
    html = build_email_html(new_banks, analyses, no_new)
    send_email(subject, html)

    # 5. Save updated state
    state["banks"] = known_banks
    state["last_run"] = datetime.now().isoformat()
    state["total_runs"] = state.get("total_runs", 0) + 1
    save_state(state)

    log.info(f"=== Done. State saved. Total banks tracked: {len(known_banks)} ===")


if __name__ == "__main__":
    main()
