"""Alert sinks, chosen by env. Every alert is also logged as one `[OPS_ALERT] {json}` line on stderr, so with no
sink configured the alert still lands in the Railway logs.

- ALERT_WEBHOOK_URL: POST {"text": "..."} (same payload as failsafe_exit_worker / BX_ALERT_WEBHOOK; Slack-compatible)
- SMTP_HOST + ALERT_EMAIL_TO: email via SMTP (STARTTLS on SMTP_PORT, default 587; SMTP_SSL=1 for implicit TLS / 465)

Sink URLs, users and passwords are never logged.
"""
from __future__ import annotations

import json
import smtplib
import sys
import urllib.request
from email.message import EmailMessage
from typing import Dict, Mapping


def _webhook(url: str, text: str, timeout: float) -> str:
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
    return "sent"


def _email(env: Mapping[str, str], subject: str, text: str, timeout: float) -> str:
    host = env["SMTP_HOST"].strip()
    port = int(env.get("SMTP_PORT") or (465 if env.get("SMTP_SSL") == "1" else 587))
    to = [a.strip() for a in env["ALERT_EMAIL_TO"].split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = (env.get("ALERT_EMAIL_FROM") or env.get("SMTP_USER") or "ops-cron@localhost").strip()
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    cls = smtplib.SMTP_SSL if env.get("SMTP_SSL") == "1" else smtplib.SMTP
    with cls(host, port, timeout=timeout) as s:
        if cls is smtplib.SMTP and env.get("SMTP_STARTTLS", "1") != "0":
            s.starttls()
        if env.get("SMTP_USER"):
            s.login(env["SMTP_USER"], env.get("SMTP_PASSWORD") or "")
        s.send_message(msg)
    return "sent"


def send(subject: str, text: str, env: Mapping[str, str], timeout: float = 15.0) -> Dict[str, str]:
    """Deliver to every configured sink. Returns {sink: "sent" | "error: ..."}; never raises."""
    out: Dict[str, str] = {}
    sys.stderr.write("[OPS_ALERT] " + json.dumps({"subject": subject, "text": text}, ensure_ascii=False) + "\n")
    out["log"] = "sent"
    if (env.get("ALERT_WEBHOOK_URL") or "").strip():
        try:
            out["webhook"] = _webhook(env["ALERT_WEBHOOK_URL"].strip(), f"{subject}\n{text}", timeout)
        except Exception as e:  # noqa: BLE001
            out["webhook"] = f"error: {type(e).__name__}"
    if (env.get("SMTP_HOST") or "").strip() and (env.get("ALERT_EMAIL_TO") or "").strip():
        try:
            out["email"] = _email(env, subject, text, timeout)
        except Exception as e:  # noqa: BLE001
            out["email"] = f"error: {type(e).__name__}: {str(e)[:120]}"
    for k, v in out.items():
        if v != "sent":
            sys.stderr.write(f"[OPS_ALERT] sink {k} failed: {v}\n")
    return out
