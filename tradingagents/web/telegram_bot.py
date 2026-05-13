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


def _parse_date_arg(args: str) -> str:
    """Resolve a /<cmd> [date] argument to a YYYY-MM-DD string.

    Accepts an empty arg (today), 'today', 'yesterday', or an explicit
    YYYY-MM-DD. Raises ValueError on garbage.
    """
    s = (args or "").strip()
    if not s or s.lower() == "today":
        return _today_ist()
    if s.lower() == "yesterday":
        from datetime import timedelta
        return (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
    datetime.strptime(s, "%Y-%m-%d")
    return s


def _cmd_help(chat_id: str, args: str) -> str:
    return (
        "<b>Trading pipeline · commands</b>\n"
        "/status [date] — capital state + lifetime P&amp;L (default: today)\n"
        "/today  [date] — plans, open + closed positions for that date\n"
        "/trades [date] — every closed trade for that date with entry/exit/P&amp;L\n"
        "/history [N]   — last N days summary (default 14)\n"
        "/help          — this message\n"
        "\n"
        "Date forms: <code>today</code> · <code>yesterday</code> · <code>YYYY-MM-DD</code>"
    )


def _cmd_status(chat_id: str, args: str) -> str:
    from tradingagents.pipeline.dispatcher import get_active_paper_trader
    from tradingagents.web.capital_service import get_today as cap_today
    from tradingagents.web.config_service import load_config
    from tradingagents.web.database import (
        get_daily_metrics,
        get_latest_capital,
        get_positions,
    )

    try:
        date = _parse_date_arg(args)
    except ValueError as e:
        return f"Bad date arg: <code>{tg._esc(args)}</code>. Use YYYY-MM-DD, today, or yesterday."

    cfg = load_config()
    seed = float(cfg.get("initial_capital", 20000))
    is_today = date == _today_ist()

    pt = get_active_paper_trader() if is_today else None
    if pt is not None:
        s = pt.get_capital_state()
        start_c, cur, free_c = s["start_capital"], s["current_value"], s["free_cash"]
        invested, pending, realized = s["invested"], s["pending_reserved"], s["realized_pnl"]
        source = "live"
    else:
        row = cap_today(date) or {}
        if row.get("start_capital") is not None:
            start_c = float(row["start_capital"])
            cur = float(row.get("capital") or start_c)
            free_c = float(row.get("free_cash") or cur)
            invested = float(row.get("invested") or 0)
            pending = float(row.get("pending_reserved") or 0)
            realized = float(row.get("daily_pnl") or 0)
            source = "finalized" if row.get("is_finalized") else "snapshot"
        else:
            start_c = get_latest_capital(default=seed, before_date=date)
            cur = start_c
            free_c = start_c
            invested = pending = realized = 0.0
            source = "no-data"

    all_metrics = get_daily_metrics()
    lifetime_pnl = sum((m.get("daily_pnl") or 0) for m in all_metrics)
    days_traded = sum(1 for m in all_metrics if m.get("is_finalized"))
    closed = [p for p in get_positions(status="closed") if p.get("pnl") is not None]
    total_trades = len(closed)
    wins = sum(1 for p in closed if (p.get("pnl") or 0) > 0)
    losses = sum(1 for p in closed if (p.get("pnl") or 0) < 0)
    win_rate = (wins / total_trades * 100.0) if total_trades else 0.0

    return (
        f"<b>Status · {date}</b>\n"
        f"<i>source: {source}</i>\n"
        f"\n"
        f"<b>Day</b>\n"
        f"• Current value: {tg.fmt_money(cur)}\n"
        f"• Start capital: {tg.fmt_money(start_c)}\n"
        f"• Free cash:     {tg.fmt_money(free_c)}\n"
        f"• Invested:      {tg.fmt_money(invested)}\n"
        f"• Pending:       {tg.fmt_money(pending)}\n"
        f"• Realized P&amp;L: {tg.fmt_pnl(realized)}\n"
        f"\n"
        f"<b>Lifetime</b>\n"
        f"• Seed capital:  {tg.fmt_money(seed)}\n"
        f"• Net P&amp;L:      {tg.fmt_pnl(lifetime_pnl)}\n"
        f"• Days traded:   {days_traded}\n"
        f"• Trades:        {total_trades} ({wins}W · {losses}L · {win_rate:.1f}% win rate)"
    )


def _cmd_today(chat_id: str, args: str) -> str:
    from tradingagents.web.database import get_positions, get_trade_plans

    try:
        date = _parse_date_arg(args)
    except ValueError:
        return f"Bad date arg: <code>{tg._esc(args)}</code>. Use YYYY-MM-DD, today, or yesterday."

    plans = get_trade_plans(date)
    open_pos = [p for p in get_positions(status="open") if p.get("date") == date]
    closed = [p for p in get_positions(status="closed") if p.get("date") == date]

    lines = [f"<b>{date}</b>", "", f"<b>Plans ({len(plans)})</b>"]
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

    lines += ["", f"<b>Closed ({len(closed)})</b>"]
    if not closed:
        lines.append("• none")
    else:
        total = 0.0
        for p in closed:
            pnl = p.get("pnl") or 0
            total += pnl
            lines.append(
                f"• {p.get('ticker')} qty {p.get('quantity')} entry "
                f"{p.get('entry_price')} → exit {p.get('exit_price')} "
                f"({p.get('exit_reason')}) {tg.fmt_pnl(pnl)}"
            )
        lines.append(f"<b>Closed total: {tg.fmt_pnl(total)}</b>")

    return "\n".join(lines)


def _cmd_trades(chat_id: str, args: str) -> str:
    from tradingagents.web.database import get_positions

    try:
        date = _parse_date_arg(args)
    except ValueError:
        return f"Bad date arg: <code>{tg._esc(args)}</code>. Use YYYY-MM-DD, today, or yesterday."

    closed = sorted(
        [p for p in get_positions(status="closed") if p.get("date") == date],
        key=lambda p: p.get("closed_at") or "",
    )
    if not closed:
        return f"<b>Trades · {date}</b>\nNo closed trades."

    lines = [f"<b>Trades · {date}</b>", ""]
    total = 0.0
    for i, p in enumerate(closed, 1):
        pnl = p.get("pnl") or 0
        total += pnl
        when = (p.get("closed_at") or "")[11:19] or "—"
        lines.append(
            f"{i}. <b>{p.get('ticker')}</b> · {when}\n"
            f"   qty {p.get('quantity')} · entry {p.get('entry_price')} → "
            f"exit {p.get('exit_price')} · {p.get('exit_reason')}\n"
            f"   P&amp;L {tg.fmt_pnl(pnl)} "
            f"({(p.get('pnl_pct') or 0):+.2f}%)"
        )
    lines += ["", f"<b>Total: {tg.fmt_pnl(total)}</b>"]
    return "\n".join(lines)


def _cmd_history(chat_id: str, args: str) -> str:
    from tradingagents.web.database import get_daily_metrics

    n = 14
    s = (args or "").strip()
    if s:
        try:
            n = max(1, min(60, int(s)))
        except ValueError:
            return f"Bad N: <code>{tg._esc(args)}</code>. Use a positive integer."

    metrics = get_daily_metrics()[:n]
    if not metrics:
        return "<b>History</b>\nNo finalized days yet."

    lines = [f"<b>History · last {len(metrics)} day(s)</b>", ""]
    total = 0.0
    for m in metrics:
        pnl = m.get("daily_pnl") or 0
        total += pnl
        flag = "" if m.get("is_finalized") else " (open)"
        lines.append(
            f"• {m.get('date')}: {tg.fmt_pnl(pnl)}  "
            f"(end {tg.fmt_money(m.get('capital'))}, "
            f"{m.get('total_trades') or 0} trades){flag}"
        )
    lines += ["", f"<b>Net: {tg.fmt_pnl(total)}</b>"]
    return "\n".join(lines)


HANDLERS: dict[str, Callable[[str, str], Optional[str]]] = {
    "/help": _cmd_help,
    "/start": _cmd_help,
    "/status": _cmd_status,
    "/today": _cmd_today,
    "/trades": _cmd_trades,
    "/history": _cmd_history,
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
