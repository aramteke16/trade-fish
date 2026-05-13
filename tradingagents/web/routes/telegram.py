"""Telegram notification endpoints.

Currently just one route — ``POST /api/telegram/test`` — which sends a
verification message using the configured token/chat_id. Useful for
confirming the bot is wired correctly before letting the pipeline drive
real notifications.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from tradingagents.dataflows.indian_market import IST
from tradingagents.web import telegram_notifier as tg
from tradingagents.web.config_service import load_config

router = APIRouter()


@router.post("/telegram/test")
def telegram_test():
    """Send a hello-world Telegram message using the current config.

    Synchronous send (not fire-and-forget) so the API can return the
    actual delivery result, not a confidence-trick 200.
    """
    cfg = load_config()
    if not cfg.get("telegram_notifications_enabled"):
        raise HTTPException(
            status_code=400,
            detail="telegram_notifications_enabled is False — flip it on first",
        )
    token = str(cfg.get("telegram_bot_token") or "").strip()
    chat_id = str(cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        raise HTTPException(
            status_code=400,
            detail="telegram_bot_token / telegram_chat_id are not set",
        )

    import requests
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "text": (
                    f"<b>Trading pipeline · test message</b>\n"
                    f"<code>{stamp} IST</code>\n"
                    f"If you can see this, notifications are wired correctly."
                ),
            },
            timeout=10,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram send failed: {e}")

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Telegram returned {r.status_code}: {r.text[:300]}",
        )
    return {
        "ok": True,
        "telegram_response": r.json(),
        "chat_id": chat_id,
        "sent_at": stamp,
    }


@router.get("/telegram/status")
def telegram_status():
    """Quick readiness probe — is the side-channel actually live?"""
    cfg = load_config()
    return {
        "enabled": bool(cfg.get("telegram_notifications_enabled", False)),
        "has_token": bool(cfg.get("telegram_bot_token")),
        "has_chat_id": bool(cfg.get("telegram_chat_id")),
        "would_send": tg.is_enabled(),
    }
