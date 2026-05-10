"""Rank screened stocks by a composite intraday score."""

import logging
from typing import List

import yfinance as yf
import pandas as pd
import numpy as np

from .filters import ScreenResult

logger = logging.getLogger(__name__)


class Ranker:
    """Rank stocks based on momentum, volume, and volatility scores."""

    def __init__(self, lookback_days: int = 30):
        self.lookback_days = lookback_days

    def rank(self, screen_results: List[ScreenResult], top_n: int = 20) -> List[dict]:
        """Return top_n ranked stocks with composite scores."""
        passed = [r for r in screen_results if r.passed]
        scored = []

        for result in passed:
            try:
                score = self._compute_score(result.ticker)
                scored.append({
                    "ticker": result.ticker,
                    "price": result.price,
                    "avg_volume_inr_crores": result.avg_volume_inr / 1e7,
                    "atr_pct": result.atr_pct * 100,
                    **score,
                })
            except Exception as e:
                logger.warning("Ranking failed for %s: %s", result.ticker, e)

        if not scored:
            return []

        df = pd.DataFrame(scored)
        # Normalize key metrics to 0-1
        for col in ["momentum_score", "volume_score", "volatility_score"]:
            cmin, cmax = df[col].min(), df[col].max()
            if cmax > cmin:
                df[f"{col}_norm"] = (df[col] - cmin) / (cmax - cmin)
            else:
                df[f"{col}_norm"] = 0.5

        df["composite_score"] = (
            df["momentum_score_norm"] * 0.4
            + df["volume_score_norm"] * 0.3
            + df["volatility_score_norm"] * 0.3
        )

        df = df.sort_values("composite_score", ascending=False)
        return df.head(top_n).to_dict(orient="records")

    def _compute_score(self, ticker: str) -> dict:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{self.lookback_days + 5}d")

        if hist.empty or len(hist) < 20:
            return {
                "momentum_score": 0,
                "volume_score": 0,
                "volatility_score": 0,
            }

        closes = hist["Close"]
        volumes = hist["Volume"]

        # Momentum: 5-day return vs 20-day return
        ret_5 = (closes.iloc[-1] - closes.iloc[-5]) / closes.iloc[-5] if len(closes) >= 5 else 0
        ret_20 = (closes.iloc[-1] - closes.iloc[-20]) / closes.iloc[-20] if len(closes) >= 20 else 0
        momentum_score = np.clip((ret_5 * 2 + ret_20) * 10, -1, 1)

        # Volume: current avg vs past avg
        vol_recent = volumes.tail(5).mean()
        vol_past = volumes.head(len(volumes) - 5).mean() if len(volumes) > 5 else vol_recent
        volume_ratio = vol_recent / vol_past if vol_past > 0 else 1
        volume_score = np.clip((volume_ratio - 1) * 2, -1, 1)

        # Volatility: ATR %
        high_low = hist["High"] - hist["Low"]
        high_close = (hist["High"] - hist["Close"].shift()).abs()
        low_close = (hist["Low"] - hist["Close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=14).mean().iloc[-1]
        atr_pct = atr / closes.iloc[-1] if closes.iloc[-1] > 0 else 0
        # Prefer 1.5% - 5% ATR for intraday
        volatility_score = np.clip(1 - abs(atr_pct * 100 - 3) / 3, -1, 1)

        return {
            "momentum_score": float(momentum_score),
            "volume_score": float(volume_score),
            "volatility_score": float(volatility_score),
        }
