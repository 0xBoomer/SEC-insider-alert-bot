"""
scoring_engine.py — Step 4: Score each surviving ticker 0–80.

Four components:
  Purchase Quality   0–30  How much relative skin-in-the-game do insiders have?
  Cluster Strength   0–20  How many insiders are buying, how tightly clustered?
                    (+5 bonus if all buys within a 7-day window)
  Price Context      0–15  Is the stock near its 52-week low (more bullish signal)?
  Earnings Proximity 0–15  How recently relative to the last earnings release?

Minimum passing score: 45 out of 80.

All market data from yfinance.  If a sub-score can't be computed (data missing),
that component returns 0 rather than crashing — the ticker can still be alerted
if other components push it above 45.
"""

import logging
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

from filter_layer import estimate_annual_comp

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_SCORE = 45            # Minimum total score to trigger an alert
CLUSTER_BONUS_DAYS = 7   # All buys within this window → +5 bonus
SWING_LOW_DAYS = 20      # Lookback for "nearest support" entry note


# ── Public entry point ────────────────────────────────────────────────────────

def score_ticker(ticker, ticker_data, survival_metrics):
    """
    Score a single ticker.  Returns a dict:
        {
            "total": int,          # 0-80 (+ possible 5pt bonus)
            "purchase_quality": int,
            "cluster_strength": int,
            "price_context": int,
            "earnings_proximity": int,
            "cluster_bonus": bool,
            "swing_low": float | None,
            "price_52w_low": float | None,
            "price_52w_high": float | None,
            "current_price": float | None,
            "passes": bool,
        }
    """
    transactions = ticker_data["transactions"]
    cluster_size = ticker_data["cluster_size"]
    cluster_insiders = ticker_data["cluster_insiders"]

    pq = _score_purchase_quality(transactions)
    cs, bonus = _score_cluster_strength(transactions, cluster_size, cluster_insiders)
    pc, price_ctx = _score_price_context(ticker)
    ep = _score_earnings_proximity(ticker)

    total = pq + cs + (5 if bonus else 0) + pc + ep
    passes = total >= MIN_SCORE

    result = {
        "total": total,
        "purchase_quality": pq,
        "cluster_strength": cs,
        "price_context": pc,
        "earnings_proximity": ep,
        "cluster_bonus": bonus,
        "passes": passes,
        **price_ctx,
    }

    logger.info(
        f"[{ticker}] Score {total}/80 "
        f"(PQ={pq} CS={cs}{'*' if bonus else ''} PC={pc} EP={ep}) "
        f"— {'ALERT' if passes else 'below threshold'}"
    )
    return result


# ── Component 1: Purchase Quality (0–30) ─────────────────────────────────────

def _score_purchase_quality(transactions):
    """
    Rewards insiders who put meaningful money on the table relative to their
    estimated pay and relative to their existing position.

    Sub-components:
      Value vs comp (0–15):   ≥ 5% = 5pts, ≥ 10% = 9pts, ≥ 25% = 13pts, ≥ 50% = 15pts
      Position increase (0–15): ≥ 10% = 5pts, ≥ 25% = 9pts, ≥ 50% = 13pts, new stake = 15pts
    """
    # Use the single largest purchase by value for this score
    best = max(transactions, key=lambda t: t["total_value"])

    comp = estimate_annual_comp(best["officer_title"])
    value_pct = best["total_value"] / comp

    # Value vs comp sub-score
    if value_pct >= 0.50:
        vs = 15
    elif value_pct >= 0.25:
        vs = 13
    elif value_pct >= 0.10:
        vs = 9
    elif value_pct >= 0.05:
        vs = 5
    else:
        vs = 2

    # Position increase sub-score
    if best["shares_before"] == 0:
        ps = 15  # New stake — maximum conviction signal
    else:
        pct = best["shares_purchased"] / best["shares_before"]
        if pct >= 0.50:
            ps = 13
        elif pct >= 0.25:
            ps = 9
        elif pct >= 0.10:
            ps = 5
        else:
            ps = 2

    return min(30, vs + ps)


# ── Component 2: Cluster Strength (0–20 + 5pt bonus) ─────────────────────────

def _score_cluster_strength(transactions, cluster_size, cluster_insiders):
    """
    More insiders buying = stronger signal (institutional knowledge is non-random).
    +5 bonus if all of TODAY's purchases fall within a 7-day window (coordinated urgency).

    Base score:
      2 insiders = 8pts
      3 insiders = 14pts
      4+ insiders = 20pts
    """
    if cluster_size >= 4:
        base = 20
    elif cluster_size == 3:
        base = 14
    else:
        base = 8  # Minimum 2 required to reach here

    # Bonus: are today's purchases tightly clustered?
    bonus = False
    if len(transactions) >= 2:
        try:
            txn_dates = [
                date.fromisoformat(t["transaction_date"][:10])
                for t in transactions
            ]
            date_range = (max(txn_dates) - min(txn_dates)).days
            if date_range <= CLUSTER_BONUS_DAYS:
                bonus = True
        except (ValueError, TypeError):
            pass

    return base, bonus


# ── Component 3: Price Context (0–15) ────────────────────────────────────────

def _score_price_context(ticker):
    """
    Insiders buying near the 52-week low is a stronger bullish signal than buying
    near the 52-week high (they're loading up when the market is most fearful).

    Score based on where the current price sits in the 52-week range:
      Bottom 10% of range  → 15pts
      Bottom 25%           → 12pts
      Bottom 50%           → 8pts
      Top 50%              → 4pts
      (near 52w high)      → 2pts

    Also returns a price_ctx dict for the alert message.
    """
    price_ctx = {
        "current_price": None,
        "price_52w_low": None,
        "price_52w_high": None,
        "swing_low": None,
    }

    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1y")

        if hist.empty:
            return 0, price_ctx

        current_price = float(hist["Close"].iloc[-1])
        high_52w = float(hist["High"].max())
        low_52w = float(hist["Low"].min())

        # 20-day swing low as a suggested entry support level
        swing_low = float(hist["Low"].iloc[-SWING_LOW_DAYS:].min()) if len(hist) >= SWING_LOW_DAYS else low_52w

        price_ctx.update({
            "current_price": round(current_price, 2),
            "price_52w_low": round(low_52w, 2),
            "price_52w_high": round(high_52w, 2),
            "swing_low": round(swing_low, 2),
        })

        # Position in range [0, 1] — 0 = at 52w low, 1 = at 52w high
        price_range = high_52w - low_52w
        if price_range <= 0:
            return 4, price_ctx  # Flat stock — neutral score

        position_in_range = (current_price - low_52w) / price_range

        if position_in_range <= 0.10:
            score = 15
        elif position_in_range <= 0.25:
            score = 12
        elif position_in_range <= 0.50:
            score = 8
        else:
            score = 4

        return score, price_ctx

    except Exception as e:
        logger.warning(f"[{ticker}] Price context error: {e}")
        return 0, price_ctx


# ── Component 4: Earnings Proximity (0–15) ────────────────────────────────────

def _score_earnings_proximity(ticker):
    """
    Insiders who buy AFTER earnings (when they've just seen fresh numbers) are
    expressing higher conviction than those buying on stale data.

    Score based on days since last earnings:
      0–14 days after earnings   → 15pts  (freshest possible signal)
      15–30 days                 → 12pts
      31–60 days                 → 8pts
      61–90 days                 → 4pts
      > 90 days or unknown       → 2pts

    We intentionally avoid penalising buys immediately before earnings because
    that would be an insider trading violation — those filings shouldn't appear.
    """
    try:
        t = yf.Ticker(ticker)
        earnings_dates = t.earnings_dates  # DataFrame, index = earnings date

        if earnings_dates is None or earnings_dates.empty:
            return 2  # No data — neutral-low score

        today = date.today()

        # Filter to past earnings dates only
        past_dates = [
            d.date() if hasattr(d, "date") else d
            for d in earnings_dates.index
            if (d.date() if hasattr(d, "date") else d) <= today
        ]

        if not past_dates:
            return 2

        most_recent = max(past_dates)
        days_since = (today - most_recent).days

        if days_since <= 14:
            return 15
        elif days_since <= 30:
            return 12
        elif days_since <= 60:
            return 8
        elif days_since <= 90:
            return 4
        else:
            return 2

    except Exception as e:
        logger.warning(f"[{ticker}] Earnings proximity error: {e}")
        return 2  # Default low score on failure
