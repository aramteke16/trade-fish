"""Telegram side-channel for live pipeline events.

A tiny fire-and-forget wrapper around the Telegram Bot HTTP API. Every call
returns immediately; the HTTP send happens on a background thread so a
slow/flapping Telegram never blocks the dispatcher's 60s tick.

Config (live, no restart needed — read from ``app_config`` on every send):
  - ``telegram_notifications_enabled`` (bool) — master toggle.
  - ``telegram_bot_token``  (str, secret) — from @BotFather.
  - ``telegram_chat_id``    (str)         — user/group/channel id.

Failure modes are intentionally silent (warning log only, never raise) so
the trading pipeline cannot crash because Telegram is down.
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

from tradingagents.dataflows.indian_market import IST

logger = logging.getLogger(__name__)


_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telegram")
_warn_lock = threading.Lock()
_last_missing_creds_warning: float = 0.0


def _load_cfg() -> dict:
    try:
        from tradingagents.web.config_service import load_config
        return load_config()
    except Exception as e:
        logger.debug("[telegram] config load failed: %s", e)
        return {}


def _do_send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning(
                "[telegram] send failed %s: %s", r.status_code, r.text[:200]
            )
    except Exception as e:
        logger.warning("[telegram] send exception: %s", e)


def is_enabled() -> bool:
    """Whether notifications would actually be sent right now."""
    cfg = _load_cfg()
    if not bool(cfg.get("telegram_notifications_enabled", False)):
        return False
    return bool(cfg.get("telegram_bot_token")) and bool(cfg.get("telegram_chat_id"))


def send_raw(text: str) -> None:
    """Fire-and-forget. Returns immediately; HTTP happens off-thread.

    No-op when the master toggle is off or credentials are missing.
    """
    cfg = _load_cfg()
    if not bool(cfg.get("telegram_notifications_enabled", False)):
        return
    token = str(cfg.get("telegram_bot_token") or "").strip()
    chat_id = str(cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        global _last_missing_creds_warning
        with _warn_lock:
            if _time.time() - _last_missing_creds_warning > 60:
                _last_missing_creds_warning = _time.time()
                logger.warning(
                    "[telegram] enabled but telegram_bot_token / telegram_chat_id "
                    "are not set in app_config"
                )
        return
    _EXECUTOR.submit(_do_send, token, chat_id, text)


def _stamp() -> str:
    return datetime.now(IST).strftime("%H:%M:%S")


def _esc(v: Any) -> str:
    """Minimal HTML escape for Telegram parse_mode=HTML."""
    s = str(v)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def notify(title: str, body: str = "", **fields: Any) -> None:
    """Send a formatted message: bold title, optional body paragraph, then
    one ``• key: value`` line per kwarg (in insertion order).

    Examples:
        notify("Precheck started", trade_date="2026-05-13")
        notify("Order REJECTED",
               ticker="RELIANCE.NS", reason="insufficient free cash")
    """
    lines: list[str] = [f"<b>{_esc(title)}</b> · <code>{_stamp()} IST</code>"]
    if body:
        lines.append(_esc(body))
    for k, v in fields.items():
        # Convert snake_case -> Title Case for readability.
        label = k.replace("_", " ")
        lines.append(f"• <b>{_esc(label)}</b>: {_esc(v)}")
    send_raw("\n".join(lines))


def notify_html(html: str) -> None:
    """For callers that want full control of the HTML body."""
    send_raw(html)


def fmt_money(n: Optional[float]) -> str:
    if n is None:
        return "—"
    sign = "" if n >= 0 else "−"
    return f"{sign}₹{abs(float(n)):,.2f}"


def fmt_pnl(n: Optional[float]) -> str:
    """Like fmt_money but always shows the sign."""
    if n is None:
        return "—"
    return f"{'+' if n >= 0 else '−'}₹{abs(float(n)):,.2f}"


# ---------------------------------------------------------------------------
# Scheduled lifecycle notifications
# ---------------------------------------------------------------------------


def _today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def notify_startup() -> None:
    """Fired once on every FastAPI lifespan startup.

    Gated by both ``telegram_notifications_enabled`` AND
    ``telegram_startup_message_enabled`` so it can be disabled
    independently of the rest of the notifier (e.g. silence reboots
    during a maintenance window).
    """
    cfg = _load_cfg()
    if not bool(cfg.get("telegram_notifications_enabled", False)):
        return
    if not bool(cfg.get("telegram_startup_message_enabled", True)):
        return

    # Best-effort context. Each lookup is wrapped so a failure (DB locked,
    # state row missing) never blocks the boot.
    host = python_ver = state = trade_date = ""
    start_cap_str = current_str = realized_str = "—"
    try:
        import platform
        import socket
        host = socket.gethostname()
        python_ver = platform.python_version()
    except Exception:  # pragma: no cover
        pass
    try:
        from tradingagents.pipeline import state_machine as sm
        sr = sm.read_state()
        state = sr.state
        trade_date = sr.trade_date or _today_ist()
    except Exception as e:
        logger.debug("[telegram] startup state lookup failed: %s", e)
        trade_date = _today_ist()
    try:
        from tradingagents.web.capital_service import get_today
        row = get_today(trade_date) or {}
        start_cap_str = fmt_money(row.get("start_capital") or row.get("capital"))
        current_str = fmt_money(row.get("capital"))
        realized_str = fmt_pnl(row.get("daily_pnl"))
    except Exception as e:
        logger.debug("[telegram] startup capital lookup failed: %s", e)

    notify(
        "Pipeline online",
        body="<i>FastAPI process started. Cron dispatcher is running.</i>",
        host=host or "unknown",
        python=python_ver or "unknown",
        state=state or "unknown",
        trade_date=trade_date,
        start_capital=start_cap_str,
        current_value=current_str,
        realized_pnl=realized_str,
    )


def notify_morning_brief(trade_date: Optional[str] = None) -> bool:
    """Post the daily morning brief once per trading day.

    Idempotent: the dispatcher records ``telegram_morning_message_last_date``
    in app_config after a successful send, and the caller is expected to
    check that before invoking this. We re-check here as a belt-and-braces.

    Returns True iff a message was actually queued for send.
    """
    cfg = _load_cfg()
    if not bool(cfg.get("telegram_notifications_enabled", False)):
        return False
    if not bool(cfg.get("telegram_morning_message_enabled", True)):
        return False

    trade_date = trade_date or _today_ist()
    last_sent = str(cfg.get("telegram_morning_message_last_date") or "")
    if last_sent == trade_date:
        return False

    # Capital context for the brief. Today's row may not exist yet at 08:00
    # (handle_waiting runs init_day at ``execution_time`` ≥ 09:30), so fall
    # back to the most recent prior EOD via get_latest_capital.
    today_start_str = "—"
    prev_eod_str = "—"
    prev_pnl_str = "—"
    prev_finalized = False
    try:
        from tradingagents.web.capital_service import get_today
        today_row = get_today(trade_date) or {}
        if today_row.get("start_capital") is not None:
            today_start_str = fmt_money(today_row.get("start_capital"))
    except Exception:
        pass
    try:
        from tradingagents.web.database import get_conn, get_latest_capital
        from tradingagents.web.config_service import load_config
        seed = float(load_config().get("initial_capital", 20000))
        expected_start = get_latest_capital(default=seed, before_date=trade_date)
        if today_start_str == "—":
            today_start_str = fmt_money(expected_start)
        # Prior-day row for context: end capital + realized P&L + finalized?
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT date, capital, daily_pnl, is_finalized FROM daily_metrics "
                "WHERE date < ? ORDER BY date DESC LIMIT 1",
                (trade_date,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            prev_eod_str = fmt_money(row["capital"])
            prev_pnl_str = fmt_pnl(row["daily_pnl"])
            prev_finalized = bool(row["is_finalized"])
    except Exception as e:
        logger.debug("[telegram] morning brief lookup failed: %s", e)

    notify(
        "Morning brief",
        body=f"<i>Good morning. Trading day {trade_date}.</i>",
        starting_capital=today_start_str,
        previous_day_end_capital=prev_eod_str,
        previous_day_pnl=prev_pnl_str,
        previous_day_finalized="yes" if prev_finalized else "no — carrying live value",
    )
    return True


# ---------------------------------------------------------------------------
# File / report attachments
# ---------------------------------------------------------------------------

# Telegram bot API hard limit for sendDocument is 50 MB. We cap a few MB
# below to leave headroom for the multipart overhead.
_TG_MAX_UPLOAD_BYTES = 49 * 1024 * 1024


def _do_send_document(token: str, chat_id: str, path: str, caption: Optional[str]) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with open(path, "rb") as f:
            files = {"document": (os.path.basename(path), f)}
            data: dict[str, str] = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            r = requests.post(url, files=files, data=data, timeout=60)
        if r.status_code != 200:
            logger.warning(
                "[telegram] sendDocument failed %s: %s",
                r.status_code, r.text[:200],
            )
    except Exception as e:
        logger.warning("[telegram] sendDocument exception: %s", e)


def send_document(path: str | Path, caption: Optional[str] = None) -> None:
    """Fire-and-forget file upload to the configured chat.

    Silently no-ops when:
      - notifications are disabled,
      - credentials are missing,
      - the path doesn't exist,
      - the file is larger than ~49 MB (Telegram's bot upload cap).
    """
    cfg = _load_cfg()
    if not bool(cfg.get("telegram_notifications_enabled", False)):
        return
    token = str(cfg.get("telegram_bot_token") or "").strip()
    chat_id = str(cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return
    p = Path(path)
    if not p.exists() or not p.is_file():
        logger.warning("[telegram] send_document: %s does not exist", p)
        return
    if p.stat().st_size > _TG_MAX_UPLOAD_BYTES:
        logger.warning(
            "[telegram] send_document: %s is %d bytes (> %d cap); skipping",
            p, p.stat().st_size, _TG_MAX_UPLOAD_BYTES,
        )
        return
    _EXECUTOR.submit(_do_send_document, token, chat_id, str(p), caption)


def send_ticker_report(ticker: str, trade_date: str, ticker_dir: str | Path) -> None:
    """Upload a ticker's ``complete_report.md`` right after analysis writes it.

    Gated by ``telegram_reports_enabled`` AND ``telegram_reports_per_ticker``.
    Picks ``complete_report.md`` (the aggregated view); falls back to nothing
    if the file is missing.
    """
    cfg = _load_cfg()
    if not bool(cfg.get("telegram_notifications_enabled", False)):
        return
    if not bool(cfg.get("telegram_reports_enabled", True)):
        return
    if not bool(cfg.get("telegram_reports_per_ticker", True)):
        return
    p = Path(ticker_dir) / "complete_report.md"
    if not p.exists():
        return
    caption = (
        f"<b>Report</b> · <code>{_esc(ticker)}</code> · <code>{_esc(trade_date)}</code>"
    )
    send_document(p, caption=caption)


def _zip_directory(src: Path, dest_zip: Path) -> bool:
    """Zip ``src`` recursively into ``dest_zip`` (DEFLATE). Returns True on
    success, False on failure or empty source."""
    if not src.exists() or not src.is_dir():
        return False
    files = [f for f in src.rglob("*") if f.is_file()]
    if not files:
        return False
    try:
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, f.relative_to(src.parent))
        return True
    except Exception as e:
        logger.warning("[telegram] zip failed for %s: %s", src, e)
        return False


def send_eod_reports_zip(trade_date: str, reports_dir: str | Path) -> None:
    """Zip ``<reports_dir>/<trade_date>/`` and upload as one attachment.

    Gated by ``telegram_reports_enabled`` AND ``telegram_reports_eod_zip``.
    Writes the zip alongside the day directory (e.g.
    ``<reports_dir>/2026-05-13.zip``) so it sticks around as an archive
    even if the upload fails or is disabled.
    """
    cfg = _load_cfg()
    if not bool(cfg.get("telegram_notifications_enabled", False)):
        return
    if not bool(cfg.get("telegram_reports_enabled", True)):
        return
    if not bool(cfg.get("telegram_reports_eod_zip", True)):
        return
    base = Path(reports_dir).expanduser()
    day_dir = base / trade_date
    if not day_dir.exists():
        logger.info("[telegram] EOD zip skipped: %s does not exist", day_dir)
        return
    zip_path = base / f"{trade_date}.zip"
    if not _zip_directory(day_dir, zip_path):
        logger.info("[telegram] EOD zip skipped: nothing to bundle for %s", trade_date)
        return
    caption = (
        f"<b>EOD report bundle</b> · <code>{_esc(trade_date)}</code>\n"
        f"All agent reports + debate transcripts for the trading day."
    )
    send_document(zip_path, caption=caption)
