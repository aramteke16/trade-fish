"""Market monitor: polls yfinance for prices during execution window.

Feeds PaperTrader.on_price_tick() every poll_interval seconds. At 15:15 IST,
triggers hard_exit_all(). Persists trade events to SQLite.

This replaces Angel One SmartAPI WebSocket for paper trading validation.
yfinance fast_info provides near-real-time prices during NSE market hours.
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Set

import yfinance as yf
import pytz

from tradingagents.execution.paper_trader import PaperTrader
from tradingagents.execution.risk_manager import (
    RiskAction,
    RiskThresholds,
    apply_trailing_stops,
)
from tradingagents.pipeline.news_monitor import NewsAction, NewsMonitor
from tradingagents.dataflows.indian_market import is_execution_window, IST
from tradingagents.web.database import (
    insert_position,
    update_position_exit,
    update_position_partial_exit,
)

logger = logging.getLogger(__name__)


class MarketMonitor:
    """Polls yfinance for current prices and feeds PaperTrader.on_price_tick().

    On every poll cycle, before forwarding prices to the trader:
      1. The trailing-stop ladder raises SLs on positions in profit.
      2. The news monitor scans fresh headlines for each invested ticker
         and forces an exit on any material catalyst (halt, downgrade,
         fraud, regulatory action). Skips when no classifier is wired.

    The mutated SLs from step 1 are honored by ``OrderManager.check_exit``
    on the same tick. The news exit from step 2 fires immediately via
    ``PaperTrader.force_exit_position``.
    """

    def __init__(
        self,
        paper_trader: PaperTrader,
        poll_interval_sec: int = 600,
        risk_thresholds: Optional[RiskThresholds] = None,
        news_monitor: Optional[NewsMonitor] = None,
    ):
        self.paper_trader = paper_trader
        self.poll_interval = poll_interval_sec
        self.risk_thresholds = risk_thresholds or RiskThresholds()
        self.news_monitor = news_monitor
        self._running = False
        # Last cycle's risk actions, exposed so callers (rich CLI) can show them.
        self._last_risk_actions: List[RiskAction] = []
        # Last cycle's news classifications, exposed for the rich display.
        self._last_news_actions: List[NewsAction] = []
        # Last cycle's per-ticker prices, exposed for mark-to-market display.
        self._last_prices: Dict[str, float] = {}
        # Dry-run state
        self._dry_run_price_idx: int = 0
        self._is_dry_run: bool = False
        # Execution window — overwritten by _reload_config() on each tick
        self._execution_window_start: str = "09:15"
        self._execution_window_end: str = "15:15"

    def run(self):
        """Block until execution window closes (15:15 IST), polling prices.

        Call this after placing orders into the paper trader. It will poll
        yfinance every poll_interval seconds and feed prices into the trader.
        """
        self._running = True
        logger.info("Market monitor started. Polling every %ds.", self.poll_interval)

        while self._running:
            now = datetime.now(IST)
            window_closed = self.tick(now)
            if window_closed:
                break
            time.sleep(self.poll_interval)

        logger.info("Market monitor stopped.")

    def stop(self):
        """Signal the monitor to stop (for external shutdown)."""
        self._running = False

    def tick(self, now: datetime) -> bool:
        """Run a single non-blocking poll cycle. Returns True iff the
        execution window has closed (i.e. caller should transition to
        analysis state and stop calling tick).

        This is the public entry-point used by the cron dispatcher
        ([dispatcher.py](dispatcher.py)). The legacy blocking ``run()``
        loop is now a thin wrapper that calls ``tick`` repeatedly with
        ``time.sleep`` between iterations — kept for the foreground CLI
        path so existing scripts don't break.

        On window close (``is_execution_window`` returns False), this
        method runs the hard-exit-all routine before returning True so
        callers don't need to hard-exit themselves.
        """
        self._reload_config()  # pull execution_window_start/end and dry_run flag from DB
        # In dry run, skip the execution-window gate so we can test outside market hours.
        if not self._is_dry_run and not is_execution_window(
            now,
            start=self._execution_window_start,
            end=self._execution_window_end,
        ):
            logger.info(
                "Execution window closed at %s. Running hard exit.",
                now.strftime("%H:%M"),
            )
            self._hard_exit_all(now)
            return True
        self._poll_cycle(now)
        return False

    def _poll_cycle(self, now: datetime):
        """Fetch prices for all tracked tickers and feed into paper trader.

        Order of operations on each poll:
          1. Fetch current prices for invested tickers (open orders + open positions).
          2. Apply the trailing-stop ladder (mutates Position.stop_loss in-place).
          3. Run the news-event monitor on open positions; if a material
             catalyst surfaces, force-exit the position immediately.
          4. Forward each price tick to PaperTrader, which now sees the raised
             SL and may trigger an exit if price pulled back below it.
        """
        tickers = self._get_tracked_tickers()
        if not tickers:
            self._last_risk_actions = []
            self._last_news_actions = []
            return

        # Per-poll config reload: pulls fresh values from the DB so a PATCH
        # to breakeven_trigger_pct / news_check_enabled / etc. takes effect
        # at the start of the next poll without restarting the pipeline.
        # Cheap (one SELECT against the small app_config table); falls back
        # to the constructor-time thresholds if the DB read fails.
        self._reload_config()

        if self._is_dry_run:
            prices = self._fetch_dry_run_prices(list(tickers))
        else:
            prices = self._fetch_current_prices(list(tickers))
        valid_prices = {t: p for t, p in prices.items() if p is not None and p > 0}
        self._last_prices = valid_prices

        # Risk-management ladder (one-way SL raise) before exit checks.
        # Pass *all* orders (including FILLED) so the parent Order.stop_loss
        # gets synced — that's the field check_exit reads on every tick.
        self._last_risk_actions = apply_trailing_stops(
            self.paper_trader.position_tracker.open_positions,
            list(self.paper_trader.order_manager.orders.values()),
            valid_prices,
            thresholds=self.risk_thresholds,
        )
        for a in self._last_risk_actions:
            logger.info(
                "[risk] %s: SL ₹%.2f → ₹%.2f (%s, unrealized %+.2f%%)",
                a.ticker, a.old_sl, a.new_sl, a.reason, a.unrealized_pct,
            )

        # News-event monitor: scan fresh headlines for each open position. On
        # a material catalyst (regulator action, fraud, halt, downgrade), the
        # classifier returns EXIT and we force-close at the current price.
        self._last_news_actions = self._evaluate_news(now, valid_prices)

        tick_count = 0
        for ticker, price in prices.items():
            if price is not None and price > 0:
                # Position may already be closed by force_exit_position above —
                # on_price_tick is a no-op for closed positions, so this is safe.
                events = self.paper_trader.on_price_tick(ticker, price, now)
                tick_count += 1
                for event in events:
                    self._handle_event(event, now)

        if tick_count > 0:
            logger.info(
                "Poll at %s: %d tickers, free_cash=%.0f, open=%d",
                now.strftime("%H:%M:%S"),
                tick_count,
                self.paper_trader.position_tracker.capital,
                len(self.paper_trader.position_tracker.open_positions),
            )

    def _reload_config(self) -> None:
        """Refresh tunable thresholds from the DB-backed config service.

        Called at the top of every poll cycle so a PATCH to
        ``breakeven_trigger_pct``, ``trail_trigger_pct``, ``trail_lock_pct``,
        ``news_check_enabled``, or ``news_check_lookback_min`` takes effect
        on the next poll without restarting the live monitor.

        Failures are logged at DEBUG (not ERROR) since the previous values
        remain valid and the next poll will retry.
        """
        try:
            from tradingagents.web.config_service import load_config
            cfg = load_config()
        except Exception as e:
            logger.debug("config reload failed (%s); keeping previous values", e)
            return

        # Risk-ladder thresholds — mutate the existing dataclass in place so
        # callers holding a reference see the new values too.
        try:
            self.risk_thresholds.breakeven_trigger_pct = float(cfg.get("breakeven_trigger_pct", self.risk_thresholds.breakeven_trigger_pct))
            self.risk_thresholds.trail_trigger_pct = float(cfg.get("trail_trigger_pct", self.risk_thresholds.trail_trigger_pct))
            self.risk_thresholds.trail_lock_pct = float(cfg.get("trail_lock_pct", self.risk_thresholds.trail_lock_pct))
        except (TypeError, ValueError) as e:
            logger.debug("risk-threshold reload skipped: %s", e)

        # News-monitor toggle + lookback. We don't tear down the news monitor
        # mid-day if it gets disabled — we just stop calling it (the
        # ``_evaluate_news`` flag check below).
        self._news_check_enabled = bool(cfg.get("news_check_enabled", True))
        if self.news_monitor is not None:
            try:
                self.news_monitor.lookback_min = int(cfg.get("news_check_lookback_min", self.news_monitor.lookback_min))
            except (TypeError, ValueError):
                pass

        # Dry-run mode: use scripted price sequence, skip market-hours gate.
        self._is_dry_run = bool(cfg.get("dry_run_e2e", False))
        self._dry_run_price_sequence = cfg.get("dry_run_price_sequence", [1400.0])

        # Execution window from DB config — governs when tick() polls vs hard-exits.
        self._execution_window_start = cfg.get("execution_window_start", "09:15")
        self._execution_window_end = cfg.get("execution_window_end", "15:15")

    def _evaluate_news(
        self, now: datetime, valid_prices: Dict[str, float]
    ) -> List[NewsAction]:
        """Run the news-event monitor on every open position. Force-exit on
        any EXIT decision; collect both EXIT and HOLD actions so the rich
        CLI can surface them. No-op when no news monitor is wired or when
        ``news_check_enabled`` was flipped off via the config API.
        """
        if self.news_monitor is None:
            return []
        if not getattr(self, "_news_check_enabled", True):
            return []

        actions: List[NewsAction] = []
        # Snapshot the dict — force_exit_position mutates open_positions.
        open_items = list(self.paper_trader.position_tracker.open_positions.items())
        for ticker, pos in open_items:
            current_price = valid_prices.get(ticker)
            if current_price is None:
                continue
            try:
                action = self.news_monitor.evaluate_position(
                    ticker=ticker,
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    stop_loss=pos.stop_loss,
                    target_1=pos.target_1,
                    now=now,
                )
            except Exception as e:
                logger.warning("News evaluation failed for %s: %s", ticker, e)
                continue
            if action is None:
                continue
            actions.append(action)
            if action.decision == "EXIT":
                logger.warning(
                    "[news] %s: forcing exit @ ₹%.2f — %s",
                    ticker, current_price, action.reason,
                )
                events = self.paper_trader.force_exit_position(
                    ticker, current_price, "news_exit", now
                )
                for event in events:
                    self._handle_event(event, now)
        return actions

    def _get_tracked_tickers(self) -> Set[str]:
        """Get all tickers we need to monitor (open orders + open positions)."""
        tickers = set()
        for order in self.paper_trader.order_manager.get_open_orders():
            tickers.add(order.ticker)
        for ticker in self.paper_trader.position_tracker.open_positions:
            tickers.add(ticker)
        return tickers

    def _fetch_dry_run_prices(self, tickers: List[str]) -> Dict[str, Optional[float]]:
        """Return the next price from the scripted sequence for all tracked tickers."""
        seq = getattr(self, "_dry_run_price_sequence", [1400.0])
        price = seq[self._dry_run_price_idx % len(seq)]
        logger.info(
            "[monitor] DRY RUN: price tick #%d = ₹%.2f for %s",
            self._dry_run_price_idx, price, tickers,
        )
        self._dry_run_price_idx += 1
        return {t: price for t in tickers}

    def _fetch_current_prices(self, tickers: List[str]) -> Dict[str, Optional[float]]:
        """Fetch last traded price for tickers via yfinance fast_info."""
        prices: Dict[str, Optional[float]] = {}
        for ticker in tickers:
            try:
                info = yf.Ticker(ticker).fast_info
                # fast_info provides lastPrice during market hours
                price = info.get("lastPrice") or info.get("regularMarketPrice") or info.get("previousClose")
                prices[ticker] = float(price) if price else None
            except Exception as e:
                logger.debug("Price fetch failed for %s: %s", ticker, e)
                prices[ticker] = None
        return prices

    def _hard_exit_all(self, now: datetime):
        """Force exit all open positions at current prices."""
        tickers = list(self.paper_trader.position_tracker.open_positions.keys())
        if not tickers:
            logger.info("No open positions to exit.")
            return

        prices = self._fetch_current_prices(tickers)
        valid_prices = {t: p for t, p in prices.items() if p is not None}

        if valid_prices:
            events = self.paper_trader.hard_exit_all(valid_prices, now)
            for event in events:
                self._handle_event(event, now)
            logger.info("Hard exit complete: %d positions closed.", len(events))
        else:
            logger.warning("Could not fetch prices for hard exit. Positions remain open.")

    def _handle_event(self, event: dict, now: datetime):
        """Log trade events, persist to database, and ping Telegram."""
        from tradingagents.web import telegram_notifier as _tg

        event_type = event.get("type", "unknown")
        ticker = event.get("ticker", "?")
        price = event.get("price")
        reason = event.get("reason", "")
        pnl = event.get("pnl")
        pnl_pct = event.get("pnl_pct")

        if event_type == "entry":
            logger.info("ENTRY: %s @ %.2f qty=%s", ticker, price, event.get("qty"))
            insert_position({
                "date": now.strftime("%Y-%m-%d"),
                "ticker": ticker,
                "entry_price": price,
                "quantity": event.get("qty"),
                "stop_loss": event.get("stop_loss"),
                "target_1": event.get("target_1"),
                "target_2": event.get("target_2"),
                "status": "open",
                "opened_at": now.isoformat(),
                "is_dry_run": self._is_dry_run,
            })
            try:
                _tg.notify(
                    "ENTRY",
                    ticker=ticker,
                    qty=event.get("qty"),
                    entry_price=_tg.fmt_money(price),
                    stop_loss=_tg.fmt_money(event.get("stop_loss")),
                    target_1=_tg.fmt_money(event.get("target_1")),
                    target_2=_tg.fmt_money(event.get("target_2")),
                )
            except Exception as e:
                logger.debug("[telegram] entry notify failed: %s", e)
        elif event_type == "partial_exit":
            pnl_str = f" P&L=Rs.{pnl:.2f}" if pnl is not None else ""
            logger.info("PARTIAL EXIT: %s @ %.2f reason=%s%s", ticker, price, reason, pnl_str)
            remaining = self.paper_trader.position_tracker.open_positions.get(ticker)
            if remaining is not None:
                update_position_partial_exit(ticker, now.strftime("%Y-%m-%d"), {
                    "quantity": remaining.quantity,
                    "stop_loss": remaining.stop_loss,
                    "target_1": remaining.target_1,
                    "target_2": remaining.target_2,
                })
            try:
                _tg.notify(
                    "PARTIAL EXIT",
                    ticker=ticker,
                    exit_price=_tg.fmt_money(price),
                    qty=event.get("qty"),
                    reason=reason,
                    pnl=_tg.fmt_pnl(pnl),
                    pnl_pct=f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—",
                    remaining_qty=(remaining.quantity if remaining else 0),
                    new_stop_loss=(_tg.fmt_money(remaining.stop_loss) if remaining else "—"),
                )
            except Exception as e:
                logger.debug("[telegram] partial_exit notify failed: %s", e)
        elif event_type == "exit":
            pnl_str = f" P&L=Rs.{pnl:.2f}" if pnl is not None else ""
            logger.info("EXIT: %s @ %.2f reason=%s%s", ticker, price, reason, pnl_str)
            update_position_exit(ticker, now.strftime("%Y-%m-%d"), {
                "exit_price": price,
                "exit_reason": reason,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "closed_at": now.isoformat(),
            })
            try:
                _tg.notify(
                    "EXIT",
                    ticker=ticker,
                    exit_price=_tg.fmt_money(price),
                    reason=reason,
                    pnl=_tg.fmt_pnl(pnl),
                    pnl_pct=f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—",
                )
            except Exception as e:
                logger.debug("[telegram] exit notify failed: %s", e)
