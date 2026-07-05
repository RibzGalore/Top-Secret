#!/usr/bin/env python3
"""
Telegram Command Listener for Thrift Conversion Monitor
=========================================================
Polls Telegram for new messages sent to your bot. When you text it a bank
name, it triggers the existing thrift_monitor.py workflow (monitor.yml)
via the GitHub Actions API with FORCE_BANK set, then replies to confirm.

This turns "go into GitHub and type a bank name" into "text your bot a
bank name" — same underlying pipeline, just a normal chat interface on
top of it.

Required environment variables (same secrets already used by monitor.yml,
plus one new one):
  TELEGRAM_BOT_TOKEN   - already have this
  TELEGRAM_CHAT_ID     - already have this (used to restrict who can trigger runs)
  GH_TOKEN             - already have this
  GITHUB_REPOSITORY    - already set automatically by GitHub Actions

Intended schedule: every 5 minutes via its own workflow (see below).
State (last processed Telegram update_id) persists via actions/cache,
same pattern as thrift_state.json.
"""

import os
import json
import logging
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
WORKFLOW_FILE = os.environ.get("MONITOR_WORKFLOW_FILE", "monitor.yml")

OFFSET_FILE = Path("telegram_offset.json")

# Commands that clearly aren't a bank name — ignore these rather than
# treating "/start" or "hello" as a request to analyze a bank called that.
IGNORE_COMMANDS = {"/start", "/help", "/status"}


def load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return json.loads(OFFSET_FILE.read_text()).get("last_update_id", 0)
        except Exception:
            pass
    return 0


def save_offset(update_id: int):
    OFFSET_FILE.write_text(json.dumps({"last_update_id": update_id}))


def get_new_messages(offset: int) -> list[dict]:
    """Fetch Telegram updates newer than `offset`. Telegram's getUpdates
    is a simple long-poll-capable endpoint; we just do a quick single call
    since this script itself runs on a short cron interval."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": offset + 1, "timeout": 5}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        log.error(f"Failed to fetch Telegram updates: {e}")
        return []


def send_reply(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
        if not resp.ok:
            log.error(f"Reply send failed: {resp.text}")
    except Exception as e:
        log.error(f"Reply send error: {e}")


def trigger_monitor_workflow(bank_name: str) -> bool:
    """Fire the existing monitor.yml via GitHub's workflow_dispatch API
    with FORCE_BANK set to the requested name."""
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {"ref": "main", "inputs": {"bank_name": bank_name}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 204:
            log.info(f"Triggered monitor.yml for '{bank_name}'")
            return True
        log.error(f"Workflow dispatch failed ({resp.status_code}): {resp.text}")
        return False
    except Exception as e:
        log.error(f"Workflow dispatch error: {e}")
        return False


def main():
    if not all([BOT_TOKEN, AUTHORIZED_CHAT_ID, GH_TOKEN, REPO]):
        raise ValueError(
            "Missing required env vars. Need TELEGRAM_BOT_TOKEN, "
            "TELEGRAM_CHAT_ID, GH_TOKEN, and GITHUB_REPOSITORY (the last "
            "one is set automatically by GitHub Actions)."
        )

    offset = load_offset()
    messages = get_new_messages(offset)

    if not messages:
        log.info("No new Telegram messages.")
        return

    highest_update_id = offset
    for update in messages:
        highest_update_id = max(highest_update_id, update.get("update_id", 0))
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        if not text:
            continue

        # Only act on messages from the chat you've already authorized via
        # TELEGRAM_CHAT_ID — this prevents a stranger who somehow finds
        # your bot from triggering runs on your repo.
        if chat_id != AUTHORIZED_CHAT_ID:
            log.warning(f"Ignoring message from unauthorized chat_id {chat_id}")
            continue

        if text.lower() in IGNORE_COMMANDS:
            if text.lower() == "/start":
                send_reply(chat_id, "Thrift Monitor bot is live. Just send me a bank name and I'll run the checklist analysis on it.")
            elif text.lower() == "/help":
                send_reply(chat_id, "Send any bank name (e.g. 'CSB Financial') to force an on-demand analysis. You'll still get the normal weekly Monday digest automatically.")
            continue

        bank_name = text
        log.info(f"Processing on-demand request: {bank_name}")
        send_reply(chat_id, f"🔍 Got it — analyzing {bank_name} now. This runs the full checklist against SEC filings; expect the report in a few minutes.")
        ok = trigger_monitor_workflow(bank_name)
        if not ok:
            send_reply(chat_id, f"⚠️ Couldn't kick off the analysis for {bank_name} — the workflow trigger failed. Check the Actions log.")

    save_offset(highest_update_id)
    log.info(f"Processed {len(messages)} message(s). Offset now {highest_update_id}.")


if __name__ == "__main__":
    main()
