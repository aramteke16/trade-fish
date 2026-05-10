"""Screening filters for Indian mid/small-cap stocks."""

import logging
from typing import List, Dict, Any
from dataclasses import dataclass

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ScreenResult:
    ticker: str
    price: float
    avg_volume_inr: float
    atr_pct: float
    passed: bool
    reasons: List[str]


class ScreenFilters:
    """Apply liquidity, volatility, and price filters to a stock universe."""

    def __init__(
        self,
        min_liquidity_inr_crores: float = 5.0,
        min_atr_pct: float = 1.5,
        min_price: float = 50.0,
        max_price: float = 50000.0,
        lookback_days: int = 30,
    ):
        self.min_liquidity = min_liquidity_inr_crores * 1e7  # convert to INR
        self.min_atr_pct = min_atr_pct / 100.0
        self.min_price = min_price
        self.max_price = max_price
        self.lookback_days = lookback_days

    def screen(self, tickers: List[str]) -> List[ScreenResult]:
        """Screen a list of tickers and return results."""
        results = []
        for ticker in tickers:
            try:
                result = self._screen_one(ticker)
                results.append(result)
            except Exception as e:
                logger.warning("Screening failed for %s: %s", ticker, e)
        return results

    def _screen_one(self, ticker: str) -> ScreenResult:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{self.lookback_days + 5}d")

        if hist.empty or len(hist) < 10:
            return ScreenResult(
                ticker=ticker, price=0, avg_volume_inr=0, atr_pct=0,
                passed=False, reasons=["Insufficient historical data"]
            )

        price = float(hist["Close"].iloc[-1])
        avg_volume = float(hist["Volume"].mean())
        avg_volume_inr = avg_volume * price

        # ATR calculation (simple)
        high_low = hist["High"] - hist["Low"]
        high_close = (hist["High"] - hist["Close"].shift()).abs()
        low_close = (hist["Low"] - hist["Close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = float(tr.rolling(window=14).mean().iloc[-1])
        atr_pct = atr / price if price > 0 else 0

        reasons = []
        passed = True

        if avg_volume_inr < self.min_liquidity:
            reasons.append(
                f"Liquidity ₹{avg_volume_inr/1e7:.2f}Cr < ₹{self.min_liquidity/1e7:.0f}Cr"
            )
            passed = False

        if atr_pct < self.min_atr_pct:
            reasons.append(f"ATR {atr_pct*100:.2f}% < {self.min_atr_pct*100:.1f}%")
            passed = False

        if price < self.min_price:
            reasons.append(f"Price ₹{price:.0f} < ₹{self.min_price:.0f}")
            passed = False

        if price > self.max_price:
            reasons.append(f"Price ₹{price:.0f} > ₹{self.max_price:.0f}")
            passed = False

        return ScreenResult(
            ticker=ticker,
            price=price,
            avg_volume_inr=avg_volume_inr,
            atr_pct=atr_pct,
            passed=passed,
            reasons=reasons if reasons else ["Passed all filters"],
        )

    def get_passed(self, results: List[ScreenResult]) -> List[ScreenResult]:
        return [r for r in results if r.passed]
