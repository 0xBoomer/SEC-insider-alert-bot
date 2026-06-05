"""
filter_layer.py — Step 2: Hard filters applied before scoring.

Four filters, all must pass:
  1. Market cap fetchable (no size cap — all market caps allowed)
  2. 2+ distinct insiders buying the same ticker within 30 days
  3. Purchase value > 5% of estimated annual compensation  OR  position +10%
  4. Not a routine same-quarter purchase vs prior year (rejects plan-driven buys)

Any ticker that fails a filter is dropped entirely.  The function returns
a dict keyed by ticker, with all passing transactions grouped together.
"""

import logging
import time
from collections import defaultdict
from datetime import date, timedelta

import requests
import yfinance as yf

from edgar_poller import fetch_recent_form4_accessions, _parse_form4, HEADERS, REQUEST_DELAY
from comp_lookup import get_executive_comp

logger = logging.getLogger(__name__)

# ── Tunable thresholds ────────────────────────────────────────────────────────
MIN_CLUSTER_SIZE = 2                  # At least 2 unique insiders
MIN_VALUE_PCT_OF_COMP = 0.05          # 5% of estimated annual comp
MIN_POSITION_INCREASE_PCT = 0.10      # OR 10% position increase
LOOKBACK_DAYS = 30                    # Cluster window
SOLO_BUY_MIN_VALUE = 250_000          # Surface single-insider buys above this amount
SOLO_SALE_MIN_VALUE = 1_000_000       # Surface single-insider sales above this amount

# Rough annual comp estimates by title keyword (proxy data without a paid API).
# These are conservative medians for small-cap companies.
COMP_ESTIMATES = {
    "chief executive": 500_000,
    "ceo": 500_000,
    "executive chairman": 450_000,
    "chief financial": 350_000,
    "cfo": 350_000,
    "chief operating": 350_000,
    "coo": 350_000,
    "chief technology": 300_000,
    "cto": 300_000,
    "chief revenue": 300_000,
    "chief marketing": 275_000,
    "president": 400_000,
    "general counsel": 275_000,
    "senior vice president": 250_000,
    "svp": 250_000,
    "vice president": 200_000,
    "vp": 200_000,
    "director": 100_000,   # Non-executive board member
    "chairman": 150_000,
    "treasurer": 175_000,
    "secretary": 150_000,
}
DEFAULT_COMP = 150_000  # Fallback for unrecognised titles


# ── Public entry point ────────────────────────────────────────────────────────

def apply_filters(transactions):
    """
    Accept the raw transaction list from edgar_poller and return a dict:
        {ticker: {"transactions": [...], "market_cap": int, "cluster_size": int}}

    Tickers failing any filter are excluded.
    """
    # Group all transactions by ticker first so we can apply per-ticker logic.
    by_ticker = defaultdict(list)
    for txn in transactions:
        by_ticker[txn["ticker"]].append(txn)

    passing = {}
    solo_buys = {}  # large single-insider buys that fail cluster filter

    for ticker, txns in by_ticker.items():
        issuer_cik = txns[0]["issuer_cik"]

        # ── Filter 1: Market cap ──────────────────────────────────────────────
        market_cap = _get_market_cap(ticker)
        if market_cap is None:
            logger.info(f"[{ticker}] SKIP — could not fetch market cap")
            continue

        # ── Filter 2: Cluster size (2+ insiders within 30 days) ──────────────
        cluster_insiders = _get_cluster_insiders(ticker, issuer_cik, txns)
        if len(cluster_insiders) < MIN_CLUSTER_SIZE:
            # Surface large single-insider buys even without a cluster
            notable = [
                t for t in txns
                if t["total_value"] >= SOLO_BUY_MIN_VALUE and _is_significant_purchase(t)
            ]
            if notable:
                solo_buys[ticker] = {
                    "transactions": sorted(notable, key=lambda t: t["total_value"], reverse=True),
                    "market_cap": market_cap,
                    "cluster_size": len(cluster_insiders),
                    "issuer_name": txns[0]["issuer_name"],
                }
                logger.info(
                    f"[{ticker}] SOLO BUY — ${max(t['total_value'] for t in notable):,.0f} "
                    f"single insider, flagged for summary"
                )
            else:
                logger.info(
                    f"[{ticker}] SKIP — only {len(cluster_insiders)} insider(s) buying "
                    f"in last {LOOKBACK_DAYS} days (need {MIN_CLUSTER_SIZE})"
                )
            continue

        # ── Filter 3: Conviction threshold (value vs comp OR position size) ───
        significant_txns = [t for t in txns if _is_significant_purchase(t)]
        if not significant_txns:
            logger.info(f"[{ticker}] SKIP — no purchases exceed conviction thresholds")
            continue

        # ── Filter 4: Routine same-quarter purchase check ────────────────────
        non_routine = [t for t in significant_txns if not _is_routine_purchase(t, issuer_cik)]
        if not non_routine:
            logger.info(f"[{ticker}] SKIP — all purchases appear routine (same quarter last year)")
            continue

        logger.info(
            f"[{ticker}] PASS — cap ${market_cap/1e6:.1f}M, "
            f"cluster {len(cluster_insiders)}, {len(non_routine)} conviction txn(s)"
        )
        passing[ticker] = {
            "transactions": non_routine,
            "market_cap": market_cap,
            "cluster_size": len(cluster_insiders),
            "cluster_insiders": cluster_insiders,
            "issuer_cik": issuer_cik,
            "issuer_name": txns[0]["issuer_name"],
        }

    return passing, solo_buys


# ── Filter helpers ────────────────────────────────────────────────────────────

def _get_market_cap(ticker):
    """Fetch market cap via yfinance. Returns None on failure."""
    try:
        info = yf.Ticker(ticker).info
        cap = info.get("marketCap") or info.get("market_cap")
        return int(cap) if cap else None
    except Exception as e:
        logger.warning(f"[{ticker}] yfinance market cap error: {e}")
        return None


def _get_cluster_insiders(ticker, issuer_cik, today_txns):
    """
    Return a set of unique insider names who made open-market purchases
    of this ticker within the last LOOKBACK_DAYS days.

    Combines today's batch with a quick EDGAR history lookup for prior days.
    """
    # Start with today's batch
    insiders = {t["filer_name"] for t in today_txns}

    # Fetch accession numbers from EDGAR submissions for this issuer
    recent_accessions = fetch_recent_form4_accessions(issuer_cik, days=LOOKBACK_DAYS)

    # Parse each historical Form 4 to find additional purchase insiders.
    # We skip accessions that are in today's batch to avoid double-counting.
    today_acc = {t["accession_no"] for t in today_txns}

    for acc_no, _ in recent_accessions:
        if acc_no in today_acc:
            continue
        try:
            hist_txns = _parse_form4(issuer_cik, acc_no)
            for t in hist_txns:
                # Only count open-market purchases (filter_layer already did code=P
                # for today; historical parse also enforces P via _extract_transactions)
                insiders.add(t["filer_name"])
        except Exception as e:
            logger.debug(f"Could not parse historical filing {acc_no}: {e}")
            continue

    return insiders


def get_comp(issuer_cik, filer_name, title):
    """
    Return annual compensation for an insider.

    Tries in order:
      1. Real comp from EDGAR DEF 14A proxy filing (most accurate)
      2. Keyword estimate table by title (fallback)

    Always returns a positive float.
    """
    real_comp = get_executive_comp(issuer_cik, filer_name)
    if real_comp:
        return real_comp
    return estimate_annual_comp(title)


def estimate_annual_comp(title):
    """
    Map an officer/director title to an estimated annual compensation figure.
    Uses a keyword lookup table; falls back to DEFAULT_COMP for unknowns.
    """
    title_lower = (title or "").lower()
    for keyword, comp in COMP_ESTIMATES.items():
        if keyword in title_lower:
            return comp
    return DEFAULT_COMP


def _is_significant_purchase(txn):
    """
    Return True if either:
      (a) purchase value > 5% of annual comp (real from DEF 14A, or estimated), OR
      (b) shares purchased increase the insider's position by ≥ 10%

    If shares_before == 0 (new stake), any purchase qualifies under (b).
    """
    comp = get_comp(txn["issuer_cik"], txn["filer_name"], txn["officer_title"])
    value_pct_of_comp = txn["total_value"] / comp

    if value_pct_of_comp >= MIN_VALUE_PCT_OF_COMP:
        return True

    # Position increase check — guard against div/0
    if txn["shares_before"] == 0:
        return True  # Building a new position is always significant

    position_increase = txn["shares_purchased"] / txn["shares_before"]
    if position_increase >= MIN_POSITION_INCREASE_PCT:
        return True

    return False


def _is_routine_purchase(txn, issuer_cik):
    """
    Return True if the same insider made a purchase in the same calendar quarter
    of the prior year — a signal of a scheduled/10b5-1 plan purchase rather than
    a conviction trade.

    We check EDGAR for a Form 4 filing by the same filer name against the same
    issuer in the matching quarter window ±15 days from a year ago.
    """
    try:
        txn_date = date.fromisoformat(txn["transaction_date"][:10])
    except (ValueError, TypeError):
        return False  # Can't parse date — don't reject

    # Define the "same quarter, prior year" window
    prior_year_date = txn_date.replace(year=txn_date.year - 1)
    window_start = (prior_year_date - timedelta(days=15)).isoformat()
    window_end = (prior_year_date + timedelta(days=15)).isoformat()

    # Check EDGAR submissions for any Form 4 in that window
    cik_padded = issuer_cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY)
        data = resp.json()
    except Exception as e:
        logger.debug(f"Routine check fetch failed for {txn['ticker']}: {e}")
        return False  # Network error — give benefit of the doubt

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    reporter_names = recent.get("reporterNames", [])  # May not be present

    filer_lower = txn["filer_name"].lower()

    for i, form in enumerate(forms):
        if form != "4":
            continue
        if i >= len(filing_dates):
            continue
        fd = filing_dates[i]
        if window_start <= fd <= window_end:
            # Best-effort: if reporterNames is available, match on name
            if reporter_names and i < len(reporter_names):
                names = reporter_names[i] if isinstance(reporter_names[i], list) else [reporter_names[i]]
                if any(filer_lower in n.lower() for n in names):
                    logger.debug(
                        f"[{txn['ticker']}] Routine pattern detected for {txn['filer_name']}"
                    )
                    return True
            else:
                # Without reporter names we can't confirm it's the same person —
                # don't reject on ambiguous evidence
                pass

    return False


# ── Sale filters ──────────────────────────────────────────────────────────────

def apply_sale_filters(sales):
    """
    Filter insider sales to notable ones: clusters (2+ unique insiders selling
    the same ticker today) or large solo sales (>= $1M).

    Returns {ticker: {"transactions": [...], "market_cap": int, "cluster_size": int,
                       "issuer_name": str}}
    """
    by_ticker = defaultdict(list)
    for txn in sales:
        by_ticker[txn["ticker"]].append(txn)

    passing = {}

    for ticker, txns in by_ticker.items():
        market_cap = _get_market_cap(ticker)
        if market_cap is None:
            continue

        significant = [t for t in txns if _is_significant_sale(t)]
        if not significant:
            continue

        unique_sellers = {t["filer_name"] for t in significant}

        if len(unique_sellers) >= MIN_CLUSTER_SIZE:
            logger.info(
                f"[{ticker}] SALE CLUSTER — cap ${market_cap/1e6:.1f}M, "
                f"{len(unique_sellers)} seller(s)"
            )
            passing[ticker] = {
                "transactions": sorted(significant, key=lambda t: t["total_value"], reverse=True),
                "market_cap": market_cap,
                "cluster_size": len(unique_sellers),
                "issuer_name": txns[0]["issuer_name"],
            }
        else:
            large_solo = [t for t in significant if t["total_value"] >= SOLO_SALE_MIN_VALUE]
            if large_solo:
                best = max(large_solo, key=lambda t: t["total_value"])
                logger.info(
                    f"[{ticker}] SOLO SALE — ${best['total_value']:,.0f} single insider"
                )
                passing[ticker] = {
                    "transactions": sorted(large_solo, key=lambda t: t["total_value"], reverse=True),
                    "market_cap": market_cap,
                    "cluster_size": 1,
                    "issuer_name": txns[0]["issuer_name"],
                }

    return passing


def _is_significant_sale(txn):
    """Sale value > 5% of comp OR sold > 10% of position."""
    comp = get_comp(txn["issuer_cik"], txn["filer_name"], txn["officer_title"])
    if txn["total_value"] / comp >= MIN_VALUE_PCT_OF_COMP:
        return True
    if txn["shares_before"] > 0:
        if txn["shares_sold"] / txn["shares_before"] >= MIN_POSITION_INCREASE_PCT:
            return True
    return False
