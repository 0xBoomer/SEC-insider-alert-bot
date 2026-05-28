"""
survival_check.py — Step 3: Financial health gate.

For each ticker that passes the filter layer, we compute three metrics:

  1. Altman Z-score  — bankruptcy predictor. Reject if Z < 1.81 (distress zone).
     Formula (original, for public companies):
       Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5
       X1 = (Current Assets - Current Liabilities) / Total Assets
       X2 = Retained Earnings / Total Assets
       X3 = EBIT / Total Assets
       X4 = Market Cap / Total Liabilities (book value)
       X5 = Revenue / Total Assets

  2. Current ratio  — short-term liquidity. Reject if < 1.0.
       Current Ratio = Current Assets / Current Liabilities

  3. Debt maturity flag — flag if ≥ 30% of total debt matures within 12 months.
     (Informational — doesn't reject, but surfaces in the alert message.)

All financials from yfinance.  If a required field is missing, we log a warning
and return None (ticker is skipped rather than crashing the run).
"""

import logging

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_ALTMAN_Z = 1.81      # Below this = financial distress zone
NEAR_MISS_Z  = 1.50      # Z in [1.50, 1.81) = near-miss: flagged in summary, not alerted
MIN_CURRENT_RATIO = 1.0  # Below this = can't cover short-term obligations
DEBT_MATURITY_FLAG_PCT = 0.30  # Flag if >30% of debt matures in <12 months


# ── Public entry point ────────────────────────────────────────────────────────

def check_survival(ticker, market_cap):
    """
    Compute survival metrics for `ticker`.

    Returns a dict on success:
        {
            "z_score": float,
            "current_ratio": float,
            "debt_maturity_flag": bool,
            "short_term_debt": float,
            "total_debt": float,
            "passes": bool,   # False means reject this ticker
            "reject_reason": str | None,
        }

    Returns None if financials cannot be fetched (ticker is skipped).
    """
    try:
        t = yf.Ticker(ticker)
        bs = t.balance_sheet          # columns = reporting dates (most recent first)
        income = t.income_stmt
        info = t.info
    except Exception as e:
        logger.warning(f"[{ticker}] yfinance fetch failed: {e}")
        return None

    if bs is None or bs.empty:
        logger.warning(f"[{ticker}] No balance sheet data")
        return None
    if income is None or income.empty:
        logger.warning(f"[{ticker}] No income statement data")
        return None

    # Use the most recent annual column (index 0)
    bs_col = bs.iloc[:, 0]
    inc_col = income.iloc[:, 0]

    try:
        total_assets = _get_bs(bs_col, ["Total Assets"])
        current_assets = _get_bs(bs_col, ["Current Assets"])
        current_liab = _get_bs(bs_col, ["Current Liabilities"])
        retained_earnings = _get_bs(
            bs_col,
            ["Retained Earnings", "Retained Earnings (Deficit)", "Accumulated Deficit"],
            allow_negative=True,
        )
        total_liab = _get_bs(
            bs_col,
            ["Total Liabilities Net Minority Interest", "Total Liabilities"],
        )

        ebit = _get_income(inc_col, ["EBIT", "Operating Income", "Operating Income Loss"])
        revenue = _get_income(inc_col, ["Total Revenue", "Net Revenue", "Revenue"])

        # Short-term / current portion of long-term debt
        short_term_debt = _get_bs(
            bs_col,
            [
                "Current Debt",
                "Current Portion Of Long Term Debt",
                "Short Term Debt",
                "Current Debt And Capital Lease Obligation",
            ],
            default=0.0,
        )
        long_term_debt = _get_bs(
            bs_col,
            [
                "Long Term Debt",
                "Long Term Debt And Capital Lease Obligation",
                "Net Long Term Debt",
            ],
            default=0.0,
        )
        total_debt = short_term_debt + long_term_debt

    except MissingDataError as e:
        logger.warning(f"[{ticker}] Missing financial field: {e}")
        return None

    # ── Guard against divide-by-zero ──────────────────────────────────────────
    if not total_assets or total_assets == 0:
        logger.warning(f"[{ticker}] Total assets is zero — cannot compute Z-score")
        return None

    # ── Altman Z-score ────────────────────────────────────────────────────────
    working_capital = current_assets - current_liab

    x1 = working_capital / total_assets
    x2 = retained_earnings / total_assets
    x3 = ebit / total_assets
    # X4 uses market cap vs book value of total liabilities
    x4 = market_cap / total_liab if total_liab and total_liab != 0 else 0.0
    x5 = revenue / total_assets

    z_score = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5

    # ── Current ratio ─────────────────────────────────────────────────────────
    current_ratio = current_assets / current_liab if current_liab and current_liab != 0 else 0.0

    # ── Debt maturity flag ────────────────────────────────────────────────────
    debt_maturity_flag = False
    if total_debt > 0:
        # Short-term debt is already due within ~12 months by definition
        debt_maturity_flag = (short_term_debt / total_debt) >= DEBT_MATURITY_FLAG_PCT

    # ── Pass/fail ─────────────────────────────────────────────────────────────
    passes = True
    reject_reason = None

    if z_score < MIN_ALTMAN_Z:
        passes = False
        reject_reason = f"Altman Z {z_score:.2f} < {MIN_ALTMAN_Z} (distress zone)"

    if current_ratio < MIN_CURRENT_RATIO:
        if passes:  # Don't overwrite an existing reason
            reject_reason = f"Current ratio {current_ratio:.2f} < {MIN_CURRENT_RATIO}"
        passes = False

    near_miss = (not passes and reject_reason and "Altman Z" in reject_reason
                 and NEAR_MISS_Z <= z_score < MIN_ALTMAN_Z)

    result = {
        "z_score": round(z_score, 2),
        "current_ratio": round(current_ratio, 2),
        "debt_maturity_flag": debt_maturity_flag,
        "short_term_debt": short_term_debt,
        "total_debt": total_debt,
        "passes": passes,
        "reject_reason": reject_reason,
        "near_miss": near_miss,
    }

    if passes:
        logger.info(
            f"[{ticker}] Survival PASS — Z={z_score:.2f}, CR={current_ratio:.2f}, "
            f"debt flag={debt_maturity_flag}"
        )
    else:
        logger.info(f"[{ticker}] Survival FAIL — {reject_reason}")

    return result


# ── Data extraction helpers ───────────────────────────────────────────────────

class MissingDataError(Exception):
    pass


def _get_bs(bs_col, field_names, default=None, allow_negative=False):
    """
    Look up a balance sheet field by trying multiple candidate names.
    Raises MissingDataError if not found and no default provided.
    """
    for name in field_names:
        if name in bs_col.index:
            val = bs_col[name]
            if pd.notna(val):
                fval = float(val)
                if allow_negative or fval != 0:
                    return fval
                if fval == 0:
                    return 0.0

    if default is not None:
        return default

    raise MissingDataError(f"None of {field_names} found in balance sheet")


def _get_income(inc_col, field_names, default=None):
    """
    Look up an income statement field by trying multiple candidate names.
    Raises MissingDataError if not found and no default provided.
    """
    for name in field_names:
        if name in inc_col.index:
            val = inc_col[name]
            if pd.notna(val):
                return float(val)

    if default is not None:
        return default

    raise MissingDataError(f"None of {field_names} found in income statement")
