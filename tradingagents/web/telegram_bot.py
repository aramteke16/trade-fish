"""Two-way Telegram bot: long-polling command handler.

A tiny worker thread that listens for ``/command`` messages from the
configured Telegram chat (the same ``telegram_chat_id`` the notifier
sends to) and replies with on-demand status information pulled from
the running pipeline / SQLite DB.

Design rationale:
  - Long polling (``getUpdates`` with ``timeout=25``) keeps this dirt simple
    and doesn't require webhooks / HTTPS / a public endpoint.
  - One background thread, started from FastAPI's lifespan, so the
    command surface comes up and down with the rest of the server.
  - Strict allow-list: only messages from the configured chat id are
    honoured. Random Telegram users can't query your portfolio.
  - Every reply is sent via the same ``telegram_notifier.send_raw`` so
    formatting and the fire-and-forget executor are reused.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import requests

from tradingagents.dataflows.indian_market import IST
from tradingagents.web import telegram_notifier as tg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command handlers
#
# Each handler takes (chat_id, args_text) and returns an HTML reply string
# (or None to skip the reply). Handlers are intentionally small — they read
# from the DB / runtime cache and format. No I/O races, no locks needed.
# ---------------------------------------------------------------------------


def _today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _cmd_help(chat_id: str, args: str) -> str:
    return (
        "<b>Trading pipeline · commands</b>\n"
        "/status — current capital, invested, today's & lifetime P&L\n"
        "/today  — today's trade plans and open positions\n"
        "/help   — this message"
    )


def _cmd_status(chat_id: str, args: str) -> str:
    """The big one: today's live capital state + lifetime aggregates."""
    from tradingagents.pipeline.dispatcher import get_active_paper_trader
    from tradingagents.web.capital_service import get_today as cap_today
    from tradingagents.web.config_service import load_config
    from tradingagents.web.database import (
        get_daily_metrics,
        get_latest_capital,
        get_positions,
    )

    cfg = load_config()
    seed = float(cfg.get("initial_capital", 20000))
    today = _today_ist()

    # Today (prefer live PaperTrader, fall back to DB snapshot, fall back to legacy)
    pt = get_active_paper_trader()
    if pt is not None:
        s = pt.get_capital_state()
        today_start = s["start_capital"]
        today_current = s["current_value"]
        today_free = s["free_cash"]
        today_invested = s["invested"]
        today_pending = s["pending_reserved"]
        today_realized = s["realized_pnl"]
        source = "live"
    else:
        row = cap_today(today) or {}
        if row.get("start_capital") is not None:
            today_start = float(row["start_capital"])
            today_current = float(row.get("capital") or today_start)
            today_free = float(row.get("free_cash") or today_current)
            today_invested = float(row.get("invested") or 0)
            today_pending = float(row.get("pending_reserved") or 0)
            today_realized = float(row.get("daily_pnl") or 0)
            source = "snapshot"
        else:
            today_start = get_latest_capital(default=seed, before_date=today)
            today_current = today_start
            today_free = today_start
            today_invested = 0.0
            today_pending = 0.0
            today_realized = 0.0
            source = "legacy"

    # Lifetime
    all_metrics = get_daily_metrics()
    lifetime_pnl = sum((m.get("daily_pnl") or 0) for m in all_metrics)
    days_traded = sum(1 for m in all_metrics if m.get("is_finalized"))
    closed = [p for p in get_positions(status="closed") if p.get("pnl") is not None]
    total_trades = len(closed)
    wins = sum(1 for p in closed if (p.get("pnl") or 0) > 0)
    losses = sum(1 for p in closed if (p.get("pnl") or 0) < 0)
    win_rate = (wins / total_trades * 100.0) if total_trades else 0.0

    return (
        f"<b>Status · {today}</b>\n"
        f"<i>capital source: {source}</i>\n"
        f"\n"
        f"<b>Today</b>\n"
        f"• Current value: {tg.fmt_money(today_current)}\n"
        f"• Start capital: {tg.fmt_money(today_start)}\n"
        f"• Free cash:     {tg.fmt_money(today_free)}\n"
        f"• Invested:      {tg.fmt_money(today_invested)}\n"
        f"• Pending:       {tg.fmt_money(today_pending)}\n"
        f"• Realized P&amp;L: {tg.fmt_pnl(today_realized)}\n"
        f"\n"
        f"<b>Lifetime</b>\n"
        f"• Seed capital:  {tg.fmt_money(seed)}\n"
        f"• Net P&amp;L:      {tg.fmt_pnl(lifetime_pnl)}\n"
        f"• Days traded:   {days_traded}\n"
        f"• Trades:        {total_trades} ({wins} wins · {losses} losses · {win_rate:.1f}% win rate)"
    )


def _cmd_today(chat_id: str, args: str) -> str:
    """Today's plans + open positions in one message."""
    from tradingagents.web.database import get_positions, get_trade_plans

    today = _today_ist()
    plans = get_trade_plans(today)
    open_pos = [p for p in get_positions(status="open") if p.get("date") == today]
    closed_today = [p for p in get_positions(status="closed") if p.get("date") == today]

    lines = [f"<b>Today · {today}</b>", "", f"<b>Plans ({len(plans)})</b>"]
    if not plans:
        lines.append("• none")
    else:
        for p in plans:
            tag = " [DRY]" if p.get("is_dry_run") else ""
            lines.append(
                f"• {p.get('ticker')} {p.get('rating')}{tag} "
                f"conf {p.get('confidence_score') or '?'}/10 — "
                f"entry {p.get('entry_zone_low')}-{p.get('entry_zone_high')} "
                f"SL {p.get('stop_loss')} T1 {p.get('target_1')}"
            )

    lines += ["", f"<b>Open positions ({len(open_pos)})</b>"]
    if not open_pos:
        lines.append("• none")
    else:
        for p in open_pos:
            lines.append(
                f"• {p.get('ticker')} qty {p.get('quantity')} @ "
                f"{p.get('entry_price')}  SL {p.get('stop_loss')} "
                f"T1 {p.get('target_1')}"
            )

    lines += ["", f"<b>Closed today ({len(closed_today)})</b>"]
    if not closed_today:
        lines.append("• none")
    else:
        for p in closed_today:
            lines.append(
                f"• {p.get('ticker')} exit @ {p.get('exit_price')} "
                f"({p.get('exit_reason')}) {tg.fmt_pnl(p.get('pnl'))}"
            )

    return "\n".join(lines)


# Registry. `/start` is what Telegram sends on first contact — alias to /help.
HANDLERS: dict[str, Callable[[str, str], Optional[str]]] = {
    "/help": _cmd_help,
    "/start": _cmd_help,
    "/status": _cmd_status,
    "/today": _cmd_today,
}


# ---------------------------------------------------------------------------
# Long-polling worker
# ---------------------------------------------------------------------------


class TelegramCommandBot:
    """One thread, one ``getUpdates`` long-poll loop, in-process command dispatch."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # offset is monotonically increasing; persisted only in memory.
        # On restart we accept all pending updates and start fresh.
        self._offset: int = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="telegram-cmd-poll", daemon=True
        )
        self._thread.start()
        logger.info("[telegram-bot] command poller started")

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        logger.info("[telegram-bot] command poller stopped")
        self._thread = None

    # ---------------------------------------------------------------- loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 — never let the loop die
                logger.warning("[telegram-bot] poll loop error: %s", e)
                # Backoff a bit on errors so we don't hammer Telegram.
                self._stop.wait(5)

    def _tick(self) -> None:
        cfg = tg._load_cfg()
        if not bool(cfg.get("telegram_notifications_enabled", False)):
            # Disabled → idle in 15s chunks; cheap and responsive to flips.
            self._stop.wait(15)
            return
        token = str(cfg.get("telegram_bot_token") or "").strip()
        allowed_chat = str(cfg.get("telegram_chat_id") or "").strip()
        if not token or not allowed_chat:
            self._stop.wait(15)
            return

        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={
                    "offset": self._offset,
                    "timeout": 25,
                    "allowed_updates": '["message","channel_post"]',
                },
                timeout=30,
            )
        except requests.RequestException as e:
            logger.debug("[telegram-bot] getUpdates network error: %s", e)
            self._stop.wait(5)
            return

        body = r.json() if r.status_code == 200 else {}
        if not body.get("ok"):
            logger.debug(
                "[telegram-bot] getUpdates non-OK: %s %s",
                r.status_code, str(body)[:200],
            )
            self._stop.wait(5)
            return

        for upd in body.get("result", []):
            self._offset = upd["update_id"] + 1
            self._dispatch(upd, allowed_chat)

    def _dispatch(self, upd: dict, allowed_chat: str) -> None:
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            return
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return

        # Allow-list. Only the configured chat is accepted.
        if chat_id != allowed_chat:
            logger.warning(
                "[telegram-bot] ignored command from non-allowed chat %s: %s",
                chat_id, text[:60],
            )
            return

        # Tokenise. Commands may include a @botname suffix (Telegram does this
        # automatically in groups/channels) — strip it.
        first, _, rest = text.partition(" ")
        cmd = first.split("@", 1)[0].lower()
        handler = HANDLERS.get(cmd)
        if handler is None:
            tg.send_raw(
                f"Unknown command: <code>{tg._esc(cmd)}</code>. Try /help."
            )
            return

        try:
            reply = handler(chat_id, rest.strip())
        except Exception as e:  # noqa: BLE001 — surface the error in chat
            logger.exception("[telegram-bot] handler %s crashed", cmd)
            tg.send_raw(
                f"<b>Command {tg._esc(cmd)} failed</b>\n"
                f"<code>{tg._esc(str(e))[:300]}</code>"
            )
            return
        if reply:
            tg.send_raw(reply)


# Module-level singleton (one poller per process is plenty).
_BOT: Optional[TelegramCommandBot] = None


def start() -> None:
    """Idempotent — safe to call from FastAPI lifespan."""
    global _BOT
    if _BOT is None:
        _BOT = TelegramCommandBot()
    _BOT.start()


def stop() -> None:
    global _BOT
    if _BOT is not None:
        _BOT.stop()
