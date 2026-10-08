"""Alert sinks. Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
If those are absent, email (SMTP_HOST + ALERT_EMAIL_TO). If neither is set, log only.

Every alert is also one `[OPS_ALERT] {json}` line on stderr. Tokens, passwords and DSNs are never logged.
"""
from __future__ import annotations

import json
import smtplib
import sys
import urllib.request
from email.message import EmailMessage
from typing import Dict, Mapping


def _scrub(text: str, env: Mapping[str, str]) -> str:
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_PASSWORD", "SMTP_USER",
                 "COCKPIT_AI_KEY", "BRAIN_DATABASE_URL", "BRAIN_DSN", "ALERT_WEBHOOK_URL"):
        val = (env.get(name) or "").strip()
        if val:
            text = text.replace(val, "***")
    return text


def _telegram(token: str, chat_id: str, text: str, timeout: float) -> str:
    # The bot token is in the URL because that is how the Telegram API is called. Never log this URL.
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
    return "sent"


def _email(env: Mapping[str, str], subject: str, text: str, timeout: float) -> str:
    host = env["SMTP_HOST"].strip()
    port = int(env.get("SMTP_PORT") or (465 if env.get("SMTP_SSL") == "1" else 587))
    to = [a.strip() for a in env["ALERT_EMAIL_TO"].split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject[:200]
    msg["From"] = (env.get("ALERT_EMAIL_FROM") or env.get("SMTP_USER") or "ops-cron@localhost").strip()
    msg["To"] = ", ".join(to)
    msg.set_content(text[:20000])
    cls = smtplib.SMTP_SSL if env.get("SMTP_SSL") == "1" else smtplib.SMTP
    with cls(host, port, timeout=timeout) as s:
        if cls is smtplib.SMTP and env.get("SMTP_STARTTLS", "1") != "0":
            s.starttls()
        if env.get("SMTP_USER"):
            s.login(env["SMTP_USER"], env.get("SMTP_PASSWORD") or "")
        s.send_message(msg)
    return "sent"


def send(subject: str, text: str, env: Mapping[str, str], timeout: float = 15.0) -> Dict[str, str]:
    """Telegram if both Telegram vars are set; otherwise email if SMTP is set; otherwise log only.

    Returns {sink: "sent" | "error: ..."}. Never raises. Never falls through to email when the
    Telegram variables are present (a failed Telegram send stays a Telegram error).
    """
    out: Dict[str, str] = {}
    safe_subject = _scrub(subject, env)
    safe_text = _scrub(text, env)
    sys.stderr.write("[OPS_ALERT] " + json.dumps({"subject": safe_subject, "text": safe_text}, ensure_ascii=False) + "\n")
    out["log"] = "sent"
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if token and chat:
        try:
            out["telegram"] = _telegram(token, chat, f"{subject}\n\n{text}", timeout)
        except Exception as e:  # noqa: BLE001
            out["telegram"] = "error: " + _scrub(f"{type(e).__name__}: {e}", env)[:160]
    elif (env.get("SMTP_HOST") or "").strip() and (env.get("ALERT_EMAIL_TO") or "").strip():
        try:
            out["email"] = _email(env, subject, text, timeout)
        except Exception as e:  # noqa: BLE001
            out["email"] = "error: " + _scrub(f"{type(e).__name__}: {e}", env)[:160]
    for k, v in out.items():
        if v != "sent":
            sys.stderr.write(f"[OPS_ALERT] sink {k} failed: {_scrub(v, env)}\n")
    return out
