"""Indian market data helpers (NSE, yfinance .NS tickers, news RSS)."""

import logging
from datetime import datetime, time
from typing import List, Optional

import feedparser
import yfinance as yf
import pandas as pd
import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

NSE_HOLIDAYS_2026 = [
    "2026-01-26", "2026-03-17", "2026-04-02", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-08-15", "2026-10-02",
    "2026-11-09", "2026-12-25",
]


def is_market_open(date: Optional[datetime] = None) -> bool:
    """Check if NSE is open on the given date."""
    if date is None:
        date = datetime.now(IST)
    date_str = date.strftime("%Y-%m-%d")
    if date_str in NSE_HOLIDAYS_2026:
        return False
    if date.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    return True


def is_execution_window(current: Optional[datetime] = None, start: str = "10:30", end: str = "15:15") -> bool:
    """Check if current time is within the intraday execution window."""
    if current is None:
        current = datetime.now(IST)
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    t = current.time()
    return time(sh, sm) <= t <= time(eh, em)


def fetch_ohlcv_batch(tickers: List[str], period: str = "3mo") -> pd.DataFrame:
    """Fetch OHLCV for multiple .NS tickers using yfinance."""
    logger.info("Fetching OHLCV for %d tickers", len(tickers))
    data = yf.download(tickers, period=period, group_by="ticker", progress=False, threads=True)
    return data


def fetch_nifty_index(period: str = "5d") -> pd.DataFrame:
    """Fetch NIFTY 50 index data."""
    return yf.Ticker("^NSEI").history(period=period)


def parse_rss_feed(url: str, max_items: int = 10) -> List[dict]:
    """Parse an RSS feed and return items."""
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            items.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": entry.get("summary", ""),
            })
        return items
    except Exception as e:
        logger.warning("RSS parse failed for %s: %s", url, e)
        return []


def fetch_moneycontrol_news(max_items: int = 10) -> List[dict]:
    url = "https://www.moneycontrol.com/rss/MCtopnews.xml"
    return parse_rss_feed(url, max_items)


def fetch_economic_times_news(max_items: int = 10) -> List[dict]:
    url = "https://economictimes.indiatimes.com/rssfeedsdefault.cms"
    return parse_rss_feed(url, max_items)


def get_fii_dii_flows() -> dict:
    """Fetch real FII/DII data via MoneyControl scraping (no credentials needed)."""
    from tradingagents.pipeline.fii_dii import fetch_fii_dii_flows
    return fetch_fii_dii_flows()


def get_fii_dii_summary() -> str:
    """Return human-readable FII/DII summary for agent prompts."""
    from tradingagents.pipeline.fii_dii import get_fii_dii_summary as _get_summary
    return _get_summary()
