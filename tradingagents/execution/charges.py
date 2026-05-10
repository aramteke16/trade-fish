"""Realistic Indian-broker intraday (MIS) charges for NSE equity.

Rates pulled from Zerodha's published charges page (https://zerodha.com/charges/).
These are exactly the costs a real intraday trader pays — using them in paper
mode means the simulated P&L is directly comparable to a real Zerodha account.

Formula breakdown for an intraday round-trip (buy + sell within the same day):

  brokerage (per leg)        = min(0.03% × turnover, ₹20)
  STT (sell side only)       = 0.025% × sell-turnover
  exchange transaction (NSE) = 0.00307% × turnover  (per leg)
  SEBI fee                   = ₹10/crore × turnover (per leg) = 0.0001%
  stamp duty (buy side only) = 0.003% × buy-turnover
  GST                        = 18% × (brokerage + exchange + SEBI)

For a typical small-cap intraday trade in our universe (~₹4-5k turnover per
side), brokerage is the ₹20 cap on each side and STT dominates the sell-side.
Round-trip total comes to roughly 0.05–0.07% of turnover for retail.
"""

from __future__ import annotations

from dataclasses import dataclass


# Zerodha rates (https://zerodha.com/charges/). Update if Zerodha publishes new ones.
BROKERAGE_PCT = 0.0003          # 0.03% per executed order
BROKERAGE_CAP_INR = 20.0        # ₹20 hard cap per executed order
STT_SELL_PCT = 0.00025          # 0.025% on sell-side turnover
EXCHANGE_TXN_PCT = 0.0000307    # NSE: 0.00307% per leg
SEBI_FEE_PCT = 0.000001         # ₹10/crore = 0.0001% per leg
STAMP_DUTY_BUY_PCT = 0.00003    # 0.003% on buy-side turnover
GST_RATE = 0.18                 # 18% GST on brokerage + exchange + SEBI


@dataclass
class ChargeBreakdown:
    """Detailed breakdown of all charges on a round-trip intraday trade.

    Useful for logging so the user can see exactly what was deducted.
    """
    brokerage: float        # both legs combined
    stt: float              # sell side only
    exchange: float         # both legs combined
    sebi: float             # both legs combined
    stamp_duty: float       # buy side only
    gst: float              # 18% on (brokerage + exchange + sebi)
    total: float            # sum of all of the above

    def __str__(self) -> str:
        return (
            f"₹{self.total:.2f} "
            f"(brokerage ₹{self.brokerage:.2f} + STT ₹{self.stt:.2f} + "
            f"exchange ₹{self.exchange:.2f} + SEBI ₹{self.sebi:.2f} + "
            f"stamp ₹{self.stamp_duty:.2f} + GST ₹{self.gst:.2f})"
        )


def round_trip_charges(buy_value: float, sell_value: float) -> ChargeBreakdown:
    """Compute the total charges on a buy + sell intraday round-trip.

    Args:
      buy_value: entry_price × quantity (₹)
      sell_value: exit_price × quantity (₹)

    Both legs go through brokerage, exchange, SEBI, GST. The buy leg adds
    stamp duty; the sell leg adds STT.
    """
    # Brokerage on each leg, capped at ₹20 each.
    brokerage_buy = min(buy_value * BROKERAGE_PCT, BROKERAGE_CAP_INR)
    brokerage_sell = min(sell_value * BROKERAGE_PCT, BROKERAGE_CAP_INR)
    brokerage = brokerage_buy + brokerage_sell

    stt = sell_value * STT_SELL_PCT
    exchange = (buy_value + sell_value) * EXCHANGE_TXN_PCT
    sebi = (buy_value + sell_value) * SEBI_FEE_PCT
    stamp_duty = buy_value * STAMP_DUTY_BUY_PCT
    gst = (brokerage + exchange + sebi) * GST_RATE
    total = brokerage + stt + exchange + sebi + stamp_duty + gst

    return ChargeBreakdown(
        brokerage=round(brokerage, 4),
        stt=round(stt, 4),
        exchange=round(exchange, 4),
        sebi=round(sebi, 4),
        stamp_duty=round(stamp_duty, 4),
        gst=round(gst, 4),
        total=round(total, 4),
    )
