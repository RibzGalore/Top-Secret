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

# ─── Configuration ────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EMAIL_FROM        = os.environ.get("EMAIL_FROM", "")
EMAIL_TO          = os.environ.get("EMAIL_TO", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_PASS         = os.environ.get("SMTP_PASS", "")
sg_key_check      = os.environ.get("SENDGRID_API_KEY", "")
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
    # Use pre-loaded financial data if available (for known banks)
    if bank.get("prefetch"):
        log.info(f"Using prefetched financial data for {bank.get('name')}")
        return bank["prefetch"]
    
    name = bank.get("name", "")
    # ... rest of function continues unchanged
    
    # If a direct source URL to a filing is provided, fetch it directly
    source = bank.get("source", "")
    if source and "sec.gov/Archives" in source:
        try:
            resp = requests.get(source, timeout=20, headers={"User-Agent": "thrift-monitor/1.0 contact@example.com"})
            if resp.ok:
                # Strip HTML tags to get clean text
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                # Remove script and style elements
                for tag in soup(["script", "style", "head"]):
                    tag.decompose()
                clean_text = soup.get_text(separator="\n")
                # Collapse whitespace
                import re
                clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
                clean_text = re.sub(r' {2,}', ' ', clean_text)
                log.info(f"Fetched filing directly for {name}: {len(clean_text)} chars after HTML strip")
                return clean_text[:50000]
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

def run_checklist_analysis(bank: dict, filing_text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
# Extract the most financially relevant section of the filing
    # Look for key financial terms and grab surrounding context
    text = filing_text
    keywords = ["total assets", "net interest income", "deposits", "net income", 
                "efficiency ratio", "tier 1", "non-performing", "allowance for credit"]
    best_start = 0
    best_score = 0
    chunk_size = 12000
    step = 2000
    for i in range(0, min(len(text) - chunk_size, 100000), step):
        chunk = text[i:i + chunk_size].lower()
        score = sum(chunk.count(kw) for kw in keywords)
        if score > best_score:
            best_score = score
            best_start = i
    best_chunk = text[best_start:best_start + chunk_size]
    
    prompt = CHECKLIST_PROMPT.format(
        bank_name=bank.get("name", "Unknown Bank"),
        source_url=bank.get("source", "SEC EDGAR"),
        filing_text=best_chunk
    )
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=CHECKLIST_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Analysis error for {bank.get('name')}: {e}")
        return {"bank_name": bank.get("name"), "score": 0, "verdict": f"Analysis failed: {e}", "checklist": [], "error": str(e)}

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

def send_email(subject: str, html_body: str, analyses: list = None):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        log.error("Telegram credentials not set")
        return

    # Build and publish HTML reports if we have analyses
    report_links = []
    if analyses:
        for analysis in analyses:
            bank_name = analysis.get("bank_name", "bank").lower()
            safe_name = "".join(c if c.isalnum() else "-" for c in bank_name).strip("-")
            date_str = datetime.now().strftime("%Y-%m-%d")
            filename = f"{safe_name}-{date_str}.html"
            html_report = build_report_html(analysis)
            url = publish_report_to_github(filename, html_report)
            if url:
                report_links.append((analysis.get("bank_name",""), url))

    # Build short Telegram summary
    import re
    clean = re.sub(r'<[^>]+>', '', html_body)
    clean = clean.replace('&nbsp;', ' ').replace('&amp;', '&').strip()
    clean = '\n'.join(line.strip() for line in clean.splitlines() if line.strip())

    if report_links:
        links_text = "\n\nFULL REPORTS:\n"
        links_text += "\n".join(f"{name}: {url}" for name, url in report_links)
        message = subject + "\n\n" + clean[:2000] + links_text
    else:
        message = subject + "\n\n" + clean[:3500]

    url = "https://api.telegram.org/bot" + bot_token + "/sendMessage"
    chunks = []
    remaining = message
    while remaining:
        if len(remaining) <= 4000:
            chunks.append(remaining)
            break
        split_at = remaining[:4000].rfind('\n')
        if split_at == -1:
            split_at = 4000
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip('\n')

    for i, chunk in enumerate(chunks):
        try:
            part_label = f"[{i+1}/{len(chunks)}]\n" if len(chunks) > 1 else ""
            resp = requests.post(url, json={
                "chat_id": chat_id,
                "text": part_label + chunk
            }, timeout=15)
            if resp.ok:
                log.info(f"Telegram message {i+1}/{len(chunks)} sent")
            else:
                log.error(f"Telegram error: {resp.text}")
        except Exception as e:
            log.error(f"Telegram error: {e}")
            raise
      
# ─── Main orchestration ────────────────────────────────────────────────────────

def publish_report_to_github(filename: str, html_content: str) -> str:
    """Commit an HTML report file to the repo and return its GitHub Pages URL."""
    gh_token = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not gh_token or not repo:
        log.warning("GH_TOKEN or GITHUB_REPOSITORY not set — skipping publish")
        return ""
    import base64
    api_url = f"https://api.github.com/repos/{repo}/contents/reports/{filename}"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    # Check if file already exists (needed to get SHA for update)
    sha = None
    try:
        check = requests.get(api_url, headers=headers, timeout=10)
        if check.ok:
            sha = check.json().get("sha")
    except Exception:
        pass
    # Encode content
    encoded = base64.b64encode(html_content.encode()).decode()
    payload = {
        "message": f"Add report: {filename}",
        "content": encoded,
        "branch": "main"
    }
    if sha:
        payload["sha"] = sha
    try:
        resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if resp.ok:
            pages_url = f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/reports/{filename}"
            log.info(f"Report published: {pages_url}")
            return pages_url
        else:
            log.error(f"GitHub publish error: {resp.text}")
            return ""
    except Exception as e:
        log.error(f"GitHub publish error: {e}")
        return ""


def build_report_html(analysis: dict) -> str:
    """Build a standalone HTML page for one bank analysis."""
    score = analysis.get("score", 0)
    rec = analysis.get("recommendation", "INSUFFICIENT_DATA")
    rec_colors = {
        "BUY_WATCH": "#1D9E75",
        "WAIT_FOR_FILINGS": "#BA7517", 
        "AVOID": "#E24B4A",
        "INSUFFICIENT_DATA": "#888780"
    }
    rec_color = rec_colors.get(rec, "#888780")
    result_colors = {"PASS": "#1D9E75", "FAIL": "#E24B4A", "CAUTION": "#BA7517", "INSUFFICIENT_DATA": "#888780"}
    icons = {"PASS": "✓", "FAIL": "✗", "CAUTION": "⚠", "INSUFFICIENT_DATA": "?"}

    rows = ""
    for item in analysis.get("checklist", []):
        result = item.get("result", "INSUFFICIENT_DATA")
        color = result_colors.get(result, "#888780")
        icon = icons.get(result, "?")
        rows += f"""
        <tr>
            <td style="padding:10px;border-bottom:1px solid #eee;text-align:center;">
                <span style="display:inline-block;width:24px;height:24px;border-radius:50%;
                background:{color}22;color:{color};text-align:center;line-height:24px;
                font-size:12px;font-weight:700;">{icon}</span>
            </td>
            <td style="padding:10px;border-bottom:1px solid #eee;font-weight:600;color:#1a1a1a;font-size:14px;">
                {item.get('number','')}. {item.get('title','')}
            </td>
            <td style="padding:10px;border-bottom:1px solid #eee;color:#555;font-size:13px;font-style:italic;">
                {item.get('metric','—')}
            </td>
            <td style="padding:10px;border-bottom:1px solid #eee;color:#555;font-size:13px;">
                {item.get('detail','')}
            </td>
        </tr>"""

    green_flags = "".join(f"<div style='margin-bottom:6px;font-size:13px;'>✓ {f}</div>" for f in analysis.get("green_flags", []))
    red_flags = "".join(f"<div style='margin-bottom:6px;font-size:13px;'>✗ {f}</div>" for f in analysis.get("red_flags", []))

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{analysis.get('bank_name','Bank')} — Thrift Monitor</title>
<style>
  body {{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#f5f5f0;margin:0;padding:20px;}}
  .card {{background:#fff;border-radius:12px;border:1px solid #e8e8e8;
    padding:24px;margin-bottom:20px;max-width:780px;margin-left:auto;margin-right:auto;}}
  .header {{display:flex;justify-content:space-between;align-items:flex-start;
    flex-wrap:wrap;gap:12px;margin-bottom:20px;}}
  .bank-name {{font-size:22px;font-weight:700;color:#1a1a1a;}}
  .bank-meta {{font-size:13px;color:#666;margin-top:4px;}}
  .score {{text-align:right;}}
  .score-num {{font-size:36px;font-weight:700;color:#1a1a1a;}}
  .rec-badge {{display:inline-block;padding:4px 14px;border-radius:6px;
    font-size:12px;font-weight:700;background:{rec_color}22;color:{rec_color};margin-top:6px;}}
  .verdict {{background:#f8f8f8;border-left:4px solid {rec_color};border-radius:0 8px 8px 0;
    padding:14px 18px;margin-bottom:16px;font-size:14px;color:#333;line-height:1.7;}}
  .analyst {{background:#f0f8f4;border-radius:8px;padding:14px 18px;
    margin-bottom:16px;font-size:13px;color:#333;line-height:1.7;}}
  table {{width:100%;border-collapse:collapse;}}
  th {{background:#f5f5f5;padding:8px 10px;text-align:left;font-size:12px;
    color:#666;font-weight:600;}}
  .flags {{display:flex;gap:20px;margin-top:16px;flex-wrap:wrap;}}
  .flag-box {{flex:1;min-width:200px;}}
  .flag-title {{font-size:11px;font-weight:700;text-transform:uppercase;
    letter-spacing:0.05em;margin-bottom:8px;}}
  .footer {{text-align:center;font-size:11px;color:#999;margin-top:20px;
    max-width:780px;margin-left:auto;margin-right:auto;}}
</style>
</head>
<body>
<div style="max-width:780px;margin:0 auto;">
  <div style="font-size:12px;font-weight:600;color:#888;text-transform:uppercase;
    letter-spacing:0.05em;margin-bottom:16px;">Community Bank Investing Agent</div>
  <div class="card">
    <div class="header">
      <div>
        <div class="bank-name">{analysis.get('bank_name','')}</div>
        <div class="bank-meta">
          {analysis.get('ticker') or 'Ticker pending'} &nbsp;·&nbsp;
          IPO {analysis.get('ipo_date') or 'TBD'} &nbsp;·&nbsp;
          Offer ${analysis.get('offer_price') or '—'}
          {(' &nbsp;·&nbsp; $' + str(analysis.get('total_assets_m')) + 'M assets') if analysis.get('total_assets_m') else ''}
        </div>
      </div>
      <div class="score">
        <div class="score-num">{score}<span style="font-size:16px;color:#888;">/10</span></div>
        <div class="rec-badge">{rec.replace('_',' ')}</div>
      </div>
    </div>
    <div class="verdict"><strong>Verdict:</strong> {analysis.get('verdict','')}</div>
    <div class="analyst">{analysis.get('analyst_take','')}</div>
    <table>
      <thead><tr>
        <th style="width:32px;"></th>
        <th>Criterion</th>
        <th>Metric</th>
        <th>Notes</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div class="flags">
      <div class="flag-box">
        <div class="flag-title" style="color:#1D9E75;">Green flags</div>
        {green_flags or '<div style="font-size:13px;color:#999;">None identified</div>'}
      </div>
      <div class="flag-box">
        <div class="flag-title" style="color:#E24B4A;">Red flags</div>
        {red_flags or '<div style="font-size:13px;color:#999;">None identified</div>'}
      </div>
    </div>
  </div>
  <div class="footer">
    Source: thezenofthriftconversions.com + SEC EDGAR · Analysis by Claude (Anthropic)<br>
    Not investment advice. Always verify figures against original filings.
  </div>
</div>
</body></html>"""

# ─── Watchlist & Price Tracking ───────────────────────────────────────────────

def fetch_current_price(ticker: str) -> dict:
    """Fetch current stock price and basic data from Yahoo Finance."""
    if not ticker:
        return {}
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.ok:
            data = resp.json()
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            return {
                "price": round(meta.get("regularMarketPrice", 0), 2),
                "prev_close": round(meta.get("chartPreviousClose", 0), 2),
                "52w_high": round(meta.get("fiftyTwoWeekHigh", 0), 2),
                "52w_low": round(meta.get("fiftyTwoWeekLow", 0), 2),
                "market_cap": meta.get("marketCap", 0),
                "fetched": datetime.now().isoformat()
            }
    except Exception as e:
        log.warning(f"Price fetch failed for {ticker}: {e}")
    return {}


def fetch_insider_buying(ticker: str, cik: str = "") -> list:
    """Check SEC EDGAR for recent Form 4 insider buying filings."""
    buys = []
    try:
        search_url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{requests.utils.quote(ticker)}%22"
            f"&forms=4&dateRange=custom&startdt={datetime.now().strftime('%Y-%m-%d')[:7]}-01"
            f"&enddt={datetime.now().strftime('%Y-%m-%d')}"
        )
        resp = requests.get(search_url, timeout=10,
                           headers={"User-Agent": "thrift-monitor/1.0 contact@example.com"})
        if resp.ok:
            hits = resp.json().get("hits", {}).get("hits", [])
            for hit in hits[:10]:
                src = hit.get("_source", {})
                buys.append({
                    "filer": src.get("display_names", ["Unknown"])[0] if src.get("display_names") else "Unknown",
                    "date": src.get("file_date", ""),
                    "form": src.get("form_type", "4")
                })
    except Exception as e:
        log.warning(f"Insider buying fetch failed for {ticker}: {e}")
    return buys[:5]


def calculate_buy_signal(bank_data: dict, price_data: dict) -> dict:
    """
    Calculate buy signal based on 5 criteria.
    Returns signal color (GREEN/YELLOW/RED) and score.
    """
    signals = []
    score = 0

    offer_price = bank_data.get("offer_price", 10)
    tbv_per_share = bank_data.get("tbv_per_share", offer_price)
    current_price = price_data.get("price", 0)
    nim = bank_data.get("nim", 0)
    nim_expanding = bank_data.get("nim_expanding", False)
    npl_ratio = bank_data.get("npl_ratio", 999)
    insider_buys = bank_data.get("recent_insider_buys", 0)
    quarters_public = bank_data.get("quarters_public", 0)
    ldr = bank_data.get("ldr", 999)

    # 1. Price vs TBV
    if current_price and tbv_per_share:
        ptbv = current_price / tbv_per_share
        if ptbv <= 1.0:
            signals.append(("Price/TBV", f"{ptbv:.2f}x — deep value", "GREEN"))
            score += 2
        elif ptbv <= 1.3:
            signals.append(("Price/TBV", f"{ptbv:.2f}x — attractive", "GREEN"))
            score += 1
        elif ptbv <= 1.5:
            signals.append(("Price/TBV", f"{ptbv:.2f}x — fair value", "YELLOW"))
        else:
            signals.append(("Price/TBV", f"{ptbv:.2f}x — expensive", "RED"))
            score -= 1
    else:
        signals.append(("Price/TBV", "Price data unavailable", "YELLOW"))

    # 2. NIM above 3% and expanding
    if nim >= 3.0 and nim_expanding:
        signals.append(("NIM", f"{nim:.2f}% — above target and expanding", "GREEN"))
        score += 2
    elif nim >= 3.0:
        signals.append(("NIM", f"{nim:.2f}% — above target, watch trend", "YELLOW"))
        score += 1
    elif nim >= 2.5:
        signals.append(("NIM", f"{nim:.2f}% — below target", "YELLOW"))
    else:
        signals.append(("NIM", f"{nim:.2f}% — well below target", "RED"))
        score -= 1

    # 3. Asset quality
    if npl_ratio < 0.5:
        signals.append(("Asset Quality", f"NPLs {npl_ratio:.2f}% — excellent", "GREEN"))
        score += 1
    elif npl_ratio < 1.0:
        signals.append(("Asset Quality", f"NPLs {npl_ratio:.2f}% — acceptable", "YELLOW"))
    else:
        signals.append(("Asset Quality", f"NPLs {npl_ratio:.2f}% — elevated", "RED"))
        score -= 1

    # 4. Insider buying
    if insider_buys >= 3:
        signals.append(("Insider Buying", f"{insider_buys} Form 4 filings — strong signal", "GREEN"))
        score += 2
    elif insider_buys >= 1:
        signals.append(("Insider Buying", f"{insider_buys} Form 4 filing(s) — mild signal", "YELLOW"))
        score += 1
    else:
        signals.append(("Insider Buying", "No recent insider buying detected", "YELLOW"))

    # 5. Track record (quarters public)
    if quarters_public >= 4:
        signals.append(("Track Record", f"{quarters_public} quarters of data — sufficient", "GREEN"))
        score += 1
    elif quarters_public >= 2:
        signals.append(("Track Record", f"{quarters_public} quarters — limited history", "YELLOW"))
    else:
        signals.append(("Track Record", f"{quarters_public} quarter(s) — too early", "RED"))
        score -= 1

    # Overall signal
    if score >= 5:
        overall = "GREEN"
        label = "Consider buying"
    elif score >= 2:
        overall = "YELLOW"
        label = "Watch closely"
    else:
        overall = "RED"
        label = "Avoid / too early"

    return {
        "overall": overall,
        "label": label,
        "score": score,
        "signals": signals,
        "ptbv": round(current_price / tbv_per_share, 2) if current_price and tbv_per_share else None
    }


def load_watchlist() -> dict:
    """Load watchlist from state file."""
    state = load_state()
    return state.get("watchlist", {})


def save_watchlist(watchlist: dict):
    """Save watchlist to state file."""
    state = load_state()
    state["watchlist"] = watchlist
    save_state(state)


def add_to_watchlist(analysis: dict, bank: dict):
    """Add or update a bank in the watchlist after analysis."""
    watchlist = load_watchlist()
    ticker = analysis.get("ticker") or bank.get("ticker", "")
    if not ticker:
        return
    offer_price = analysis.get("offer_price", 10) or 10
    # Derive TBV from checklist item 7 metric if possible
    tbv_per_share = offer_price  # default to offer price as TBV proxy
    watchlist[ticker] = {
        "name": analysis.get("bank_name", ""),
        "ticker": ticker,
        "offer_price": offer_price,
        "tbv_per_share": tbv_per_share,
        "checklist_score": analysis.get("score", 0),
        "recommendation": analysis.get("recommendation", ""),
        "nim": 0,
        "nim_expanding": False,
        "npl_ratio": 0,
        "ldr": 0,
        "recent_insider_buys": 0,
        "quarters_public": 0,
        "ipo_date": analysis.get("ipo_date", ""),
        "added": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
        "report_url": "",
        "notes": analysis.get("verdict", "")
    }
    save_watchlist(watchlist)
    log.info(f"Added {ticker} to watchlist")


def build_watchlist_html(watchlist: dict) -> str:
    """Build the live watchlist dashboard HTML page."""
    run_date = datetime.now().strftime("%B %d, %Y")

    signal_colors = {
        "GREEN": {"bg": "#f0faf5", "border": "#1D9E75", "text": "#1D9E75", "dot": "#1D9E75"},
        "YELLOW": {"bg": "#fffbf0", "border": "#BA7517", "text": "#BA7517", "dot": "#BA7517"},
        "RED": {"bg": "#fff5f5", "border": "#E24B4A", "text": "#E24B4A", "dot": "#E24B4A"}
    }

    cards = ""
    for ticker, bank in watchlist.items():
        price_data = fetch_current_price(ticker)
        insider_buys = fetch_insider_buying(ticker)
        bank["recent_insider_buys"] = len(insider_buys)

        # Estimate quarters public
        if bank.get("ipo_date"):
            try:
                from datetime import date
                ipo = datetime.fromisoformat(bank["ipo_date"])
                quarters = max(0, int((datetime.now() - ipo).days / 90))
                bank["quarters_public"] = quarters
            except Exception:
                bank["quarters_public"] = 0

        signal = calculate_buy_signal(bank, price_data)
        sc = signal_colors.get(signal["overall"], signal_colors["YELLOW"])

        current_price = price_data.get("price", 0)
        ptbv = signal.get("ptbv", "N/A")
        price_display = f"${current_price:.2f}" if current_price else "N/A"
        ptbv_display = f"{ptbv:.2f}x" if isinstance(ptbv, float) else "N/A"

        signal_rows = ""
        for sig_name, sig_detail, sig_color in signal.get("signals", []):
            c = signal_colors.get(sig_color, signal_colors["YELLOW"])
            signal_rows += f"""
            <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;
                border-bottom:1px solid #f0f0f0;">
                <div style="width:10px;height:10px;border-radius:50%;background:{c['dot']};
                    flex-shrink:0;margin-top:3px;"></div>
                <div>
                    <div style="font-size:12px;font-weight:600;color:#1a1a1a;">{sig_name}</div>
                    <div style="font-size:12px;color:#555;">{sig_detail}</div>
                </div>
            </div>"""

        insider_html = ""
        if insider_buys:
            insider_html = "<div style='margin-top:10px;font-size:11px;color:#666;'>Recent Form 4s: "
            insider_html += ", ".join(f"{b['filer']} ({b['date']})" for b in insider_buys[:3])
            insider_html += "</div>"

        report_link = f'<a href="{bank.get("report_url","")}" style="font-size:12px;color:#4A90D9;">View full report →</a>' if bank.get("report_url") else ""

        cards += f"""
        <div style="background:{sc['bg']};border:1.5px solid {sc['border']};border-radius:12px;
            padding:20px 24px;margin-bottom:20px;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;
                flex-wrap:wrap;gap:12px;margin-bottom:16px;">
                <div>
                    <div style="font-size:18px;font-weight:700;color:#1a1a1a;">{bank.get('name','')}</div>
                    <div style="font-size:13px;color:#666;margin-top:3px;">
                        {ticker} &nbsp;·&nbsp; IPO {bank.get('ipo_date','TBD')} @ ${bank.get('offer_price',10):.2f}
                        &nbsp;·&nbsp; Checklist score: {bank.get('checklist_score',0)}/10
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:28px;font-weight:700;color:{sc['text']};">
                        {price_display}
                    </div>
                    <div style="font-size:12px;color:#666;margin-top:2px;">
                        P/TBV: {ptbv_display} &nbsp;·&nbsp;
                        52w: ${price_data.get('52w_low',0):.2f}–${price_data.get('52w_high',0):.2f}
                    </div>
                </div>
            </div>

            <div style="display:inline-block;padding:6px 16px;border-radius:20px;
                background:{sc['border']};color:#fff;font-size:13px;font-weight:700;
                margin-bottom:16px;">
                {signal['label']} &nbsp;·&nbsp; Signal score: {signal['score']}/8
            </div>

            <div style="background:#fff;border-radius:8px;padding:14px 16px;margin-bottom:12px;">
                {signal_rows}
            </div>

            <div style="font-size:12px;color:#555;line-height:1.6;font-style:italic;">
                {bank.get('notes','')[:200]}{'...' if len(bank.get('notes','')) > 200 else ''}
            </div>
            {insider_html}
            <div style="margin-top:12px;">{report_link}</div>
        </div>"""

    if not watchlist:
        cards = """
        <div style="text-align:center;padding:40px;color:#888;font-size:14px;">
            No banks in watchlist yet. Banks will appear here after the monitor detects
            new thrift conversions.
        </div>"""

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thrift Monitor — Live Watchlist</title>
<style>
  body {{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#f5f5f0;margin:0;padding:20px;}}
  .container {{max-width:780px;margin:0 auto;}}
</style>
</head>
<body>
<div class="container">
  <div style="margin-bottom:24px;">
    <div style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;
        letter-spacing:0.06em;margin-bottom:6px;">Community Bank Investing Agent</div>
    <div style="font-size:24px;font-weight:700;color:#1a1a1a;">Live Watchlist</div>
    <div style="font-size:13px;color:#888;margin-top:4px;">
        Updated {run_date} &nbsp;·&nbsp; {len(watchlist)} bank{'s' if len(watchlist) != 1 else ''} tracked
    </div>
  </div>

  <div style="background:#fff;border-radius:10px;padding:14px 18px;margin-bottom:24px;
      border:1px solid #e8e8e8;font-size:13px;color:#555;line-height:1.7;">
    <strong style="color:#1a1a1a;">Buy signal logic:</strong>
    GREEN = Price ≤1.3x TBV + NIM ≥3% expanding + NPLs &lt;1% + insider buying + 4+ quarters data.
    Each factor scored — 5+ points = consider buying, 2-4 = watch, below 2 = avoid.
  </div>

  {cards}

  <div style="text-align:center;font-size:11px;color:#aaa;margin-top:24px;padding-top:16px;
      border-top:1px solid #e8e8e8;">
    Prices from Yahoo Finance · Insider data from SEC EDGAR Form 4 filings<br>
    Not investment advice. Always verify before trading.
  </div>
</div>
</body></html>"""

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

    log.info(f"New banks detected: {len(new_banks)}")

    # 3. For each new bank, fetch prospectus and run checklist
   analyses = []
    for bank in new_banks:
        log.info(f"Analyzing: {bank['name']}")
        filing_text = fetch_prospectus_text(bank)
        analysis = run_checklist_analysis(bank, filing_text)
        analyses.append(analysis)
        log.info(f"  Score: {analysis.get('score', '?')}/10 — {analysis.get('recommendation', '?')}")
        # Add to watchlist if it has a ticker
        if analysis.get("ticker") or bank.get("ticker"):
            if not analysis.get("ticker") and bank.get("ticker"):
                analysis["ticker"] = bank["ticker"]
            add_to_watchlist(analysis, bank)
    
    # 4. Build and send the weekly email
    no_new = len(new_banks) == 0
    subject = (
        f"[Thrift Monitor] No new additions this week — {datetime.now().strftime('%b %d')}"
        if no_new else
        f"[Thrift Monitor] {len(new_banks)} new bank{'s' if len(new_banks) > 1 else ''} detected — {datetime.now().strftime('%b %d')}"
    )
    html = build_email_html(new_banks, analyses, no_new)
    send_email(subject, html, analyses if not no_new else None)

    # 5. Save updated state
    state["banks"] = known_banks
    state["last_run"] = datetime.now().isoformat()
    state["total_runs"] = state.get("total_runs", 0) + 1
    save_state(state)

    # Update and publish live watchlist dashboard
    watchlist = load_watchlist()
    if watchlist:
        watchlist_html = build_watchlist_html(watchlist)
        watchlist_url = publish_report_to_github("watchlist.html", watchlist_html)
        if watchlist_url:
            log.info(f"Watchlist published: {watchlist_url}")
    log.info(f"=== Done. State saved. Total banks tracked: {len(known_banks)} ===")


if __name__ == "__main__":
    main()
