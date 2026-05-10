"""Fetch FII/DII daily activity data from MoneyControl (public, no auth).

Strategy:
1. Scrape MoneyControl's FII/DII activity page
2. Cache results to ~/.tradingagents/cache/fii_dii/latest.json
3. On failure, return cached previous day's data with stale flag
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "fii_dii"
_MONEYCONTROL_URL = "https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/data.html"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}


def fetch_fii_dii_flows(date: Optional[str] = None) -> dict:
    """Fetch FII/DII net buy/sell data.

    Returns:
        {
            "date": "2026-05-07",
            "fii_net_crores": -1234.5,  # negative = net selling
            "dii_net_crores": 890.3,    # positive = net buying
            "fii_buy_crores": 5000.0,
            "fii_sell_crores": 6234.5,
            "dii_buy_crores": 4890.3,
            "dii_sell_crores": 4000.0,
            "source": "moneycontrol",
            "stale": False,
        }
    """
    result = _try_moneycontrol()
    if result:
        _cache_result(result)
        return result

    # Fallback to cached
    cached = _load_cached()
    if cached:
        cached["stale"] = True
        return cached

    return {
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "fii_net_crores": None,
        "dii_net_crores": None,
        "source": "unavailable",
        "stale": True,
    }


def get_fii_dii_summary() -> str:
    """Return a human-readable summary for the institutional sentiment analyst."""
    data = fetch_fii_dii_flows()
    if data.get("fii_net_crores") is None:
        return "FII/DII flow data unavailable for today."

    fii_direction = "buying" if data["fii_net_crores"] > 0 else "selling"
    dii_direction = "buying" if data["dii_net_crores"] > 0 else "selling"
    stale_note = " (data may be from previous session)" if data.get("stale") else ""

    lines = [
        f"FII/DII Activity{stale_note}:",
        f"- FII: Net {fii_direction} Rs.{abs(data['fii_net_crores']):.0f} Cr",
        f"- DII: Net {dii_direction} Rs.{abs(data['dii_net_crores']):.0f} Cr",
    ]
    if data.get("fii_buy_crores") is not None:
        lines.append(f"- FII Buy: Rs.{data['fii_buy_crores']:.0f} Cr | Sell: Rs.{data['fii_sell_crores']:.0f} Cr")
    if data.get("dii_buy_crores") is not None:
        lines.append(f"- DII Buy: Rs.{data['dii_buy_crores']:.0f} Cr | Sell: Rs.{data['dii_sell_crores']:.0f} Cr")
    lines.append(f"Source: {data['source']}")
    return "\n".join(lines)


def _try_moneycontrol() -> Optional[dict]:
    """Scrape MoneyControl FII/DII activity page."""
    try:
        resp = requests.get(_MONEYCONTROL_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # MoneyControl page has tables with FII and DII data
        # Look for patterns like: <td>FII/FPI</td><td>Buy</td><td>Sell</td><td>Net</td>
        # The actual numbers are in subsequent rows

        fii_data = _extract_category(html, "FII")
        dii_data = _extract_category(html, "DII")

        if fii_data or dii_data:
            return {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "fii_net_crores": fii_data.get("net") if fii_data else None,
                "dii_net_crores": dii_data.get("net") if dii_data else None,
                "fii_buy_crores": fii_data.get("buy") if fii_data else None,
                "fii_sell_crores": fii_data.get("sell") if fii_data else None,
                "dii_buy_crores": dii_data.get("buy") if dii_data else None,
                "dii_sell_crores": dii_data.get("sell") if dii_data else None,
                "source": "moneycontrol",
                "stale": False,
            }
    except Exception as e:
        logger.warning("MoneyControl FII/DII scrape failed: %s", e)
    return None


def _extract_category(html: str, category: str) -> Optional[dict]:
    """Extract buy/sell/net values for FII or DII from page HTML.

    MoneyControl uses various table formats. We try multiple patterns:
    1. Look for rows with the category name followed by numeric cells
    2. Look for specific CSS class patterns
    """
    # Pattern 1: Table rows with category identifier
    # Matches: <td>FII/FPI</td>...<td>12,345.67</td>...<td>11,234.56</td>...<td>1,111.11</td>
    pattern = (
        rf"(?:>|{category}[/\w]*</td>)"
        r".*?<td[^>]*>\s*([\d,]+\.?\d*)\s*</td>"
        r".*?<td[^>]*>\s*([\d,]+\.?\d*)\s*</td>"
        r".*?<td[^>]*>\s*(-?[\d,]+\.?\d*)\s*</td>"
    )

    # Search near the category keyword
    cat_idx = html.upper().find(category.upper())
    if cat_idx == -1:
        return None

    # Search in a window around the category mention
    search_window = html[max(0, cat_idx - 100):cat_idx + 2000]

    # Try to find 3 consecutive numbers (buy, sell, net)
    numbers = re.findall(r">([-\d,]+\.?\d*)<", search_window)
    crore_values = []
    for n in numbers:
        try:
            val = float(n.replace(",", ""))
            # FII/DII values are typically in range 1000-100000 crores
            if 100 <= abs(val) <= 200000:
                crore_values.append(val)
        except ValueError:
            continue

    if len(crore_values) >= 3:
        # First two large numbers are usually buy and sell, third is net
        buy = crore_values[0]
        sell = crore_values[1]
        net = crore_values[2] if len(crore_values) > 2 else buy - sell
        return {"buy": buy, "sell": sell, "net": net}

    if len(crore_values) >= 2:
        buy = crore_values[0]
        sell = crore_values[1]
        return {"buy": buy, "sell": sell, "net": buy - sell}

    return None


def _cache_result(data: dict):
    """Cache the latest result to disk."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / "latest.json"
    cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_cached() -> Optional[dict]:
    """Load the last cached result."""
    cache_file = _CACHE_DIR / "latest.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None
