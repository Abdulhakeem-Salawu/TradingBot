"""Telegram notifications.

Set TELEGRAM_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID. Without them,
messages are printed only, so the runner works before notifications are set up.
A failed notification is logged and never stops the ledger update.
"""

from __future__ import annotations

import html
import os

TELEGRAM_LIMIT = 4000   # API maximum is 4096 characters per message


def send(text: str, pre: bool = False) -> bool:
    """Send a message. pre=True renders it monospaced, so tables stay aligned."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set -- not sending)")
        return False

    import requests

    ok = True
    # HTML escaping lengthens text ("->" becomes "-&gt;"), so leave headroom.
    for chunk in _chunks(text, 3000 if pre else TELEGRAM_LIMIT):
        data = {"chat_id": chat_id, "text": chunk}
        if pre:
            data = {"chat_id": chat_id, "parse_mode": "HTML", "text": f"<pre>{html.escape(chunk)}</pre>"}
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data=data, timeout=20)
            if r.status_code != 200:
                # Never print the URL: it contains the bot token.
                print(f"telegram send failed: HTTP {r.status_code} {r.text[:200]}")
                ok = False
        except requests.RequestException as e:
            print(f"telegram send failed: {type(e).__name__}")
            ok = False
    return ok


def _chunks(text: str, limit: int):
    """Split on line boundaries so a table row is never cut in half."""
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit and buf:
            yield buf
            buf = ""
        buf += line
    if buf:
        yield buf
