"""
dryrun.py — Run the full pipeline for the last N days, print results, no Telegram.
"""

import logging
import sys

import edgar_poller
import filter_layer
import scoring_engine
import survival_check

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

DAYS_BACK = 10

print(f"\n{'='*60}")
print(f"DRY RUN — last {DAYS_BACK} days of Form 4 filings")
print(f"{'='*60}\n")

# Step 1: Pull filings
print(f"Polling EDGAR for last {DAYS_BACK} days…")
transactions, sale_transactions = edgar_poller.fetch_form4_filings(days_back=DAYS_BACK)
print(f"Found {len(transactions)} purchases and {len(sale_transactions)} sales\n")

if not transactions:
    print("No transactions found.")
    sys.exit(0)

# Step 2: Filters
print("Applying filters…")
logging.getLogger("filter_layer").setLevel(logging.INFO)
filtered, solo_buys = filter_layer.apply_filters(transactions)
logging.getLogger("filter_layer").setLevel(logging.WARNING)
print(f"\n{len(filtered)} ticker(s) passed all filters\n")

if solo_buys:
    print(f"{'─'*60}")
    print(f"SOLO BUYS — large single-insider purchases ({len(solo_buys)}):")
    for ticker, data in sorted(solo_buys.items(), key=lambda x: -x[1]["transactions"][0]["total_value"]):
        best = data["transactions"][0]
        cap_m = data["market_cap"] / 1e6
        print(f"  {ticker}: {best['filer_name']} ({best['officer_title'] or 'Director'}): "
              f"{best['shares_purchased']:,.0f} sh @ ${best['price_per_share']:.2f} "
              f"= ${best['total_value']:,.0f}  |  Cap ${cap_m:.1f}M")
    print()

if not filtered:
    print("No tickers passed filters.")
    sys.exit(0)

# Steps 3-4: Survival + scoring
alerts = []
skipped = []

for ticker, ticker_data in filtered.items():
    survival = survival_check.check_survival(ticker, ticker_data["market_cap"])
    if survival is None:
        skipped.append((ticker, "could not fetch financials"))
        continue
    if not survival["passes"]:
        skipped.append((ticker, survival["reject_reason"]))
        continue

    score_result = scoring_engine.score_ticker(ticker, ticker_data, survival)
    alerts.append((ticker, ticker_data, score_result, survival))

# Print skipped
if skipped:
    print(f"{'─'*60}")
    print(f"REJECTED by survival check or scoring ({len(skipped)}):")
    for ticker, reason in skipped:
        print(f"  {ticker}: {reason}")

# Print alerts sorted by score
alerts.sort(key=lambda x: x[2]["total"], reverse=True)

print(f"\n{'='*60}")
print(f"ALERTS ({len(alerts)} qualifying tickers)")
print(f"{'='*60}")

for ticker, ticker_data, score_result, survival in alerts:
    txns = ticker_data["transactions"]
    cap_m = ticker_data["market_cap"] / 1e6
    total = score_result["total"]
    passes = score_result["passes"]

    flag = "✅ ALERT" if passes else f"❌ score {total} < 45"

    print(f"\n{flag} — ${ticker}  |  Score {total}/80  |  Cap ${cap_m:.1f}M")
    print(f"  PQ={score_result['purchase_quality']} "
          f"CS={score_result['cluster_strength']}{'*' if score_result['cluster_bonus'] else ''} "
          f"PC={score_result['price_context']} "
          f"EP={score_result['earnings_proximity']}")
    print(f"  Cluster: {ticker_data['cluster_size']} insider(s)")
    for t in sorted(txns, key=lambda x: x["total_value"], reverse=True):
        shares_after = t["shares_after"]
        cp = score_result.get("current_price") or t["price_per_share"]
        pos_val = shares_after * cp
        print(f"    • {t['filer_name']} ({t['officer_title'] or 'Director'}): "
              f"{t['shares_purchased']:,.0f} sh @ ${t['price_per_share']:.2f} "
              f"= ${t['total_value']:,.0f}  |  txn date: {t['transaction_date']}")
        print(f"      Total position: {shares_after:,.0f} sh (~${pos_val/1e6:.1f}M)")
    print(f"  Survival: Z={survival['z_score']} | CR={survival['current_ratio']} | "
          f"Debt flag={survival['debt_maturity_flag']}")
    cp = score_result.get("current_price")
    low52 = score_result.get("price_52w_low")
    high52 = score_result.get("price_52w_high")
    swing = score_result.get("swing_low")
    if cp and low52 and high52:
        rng = high52 - low52
        pos_pct = (cp - low52) / rng * 100 if rng > 0 else 50
        print(f"  Price: ${cp} | 52W Low ${low52} | 52W High ${high52} "
              f"| bottom {pos_pct:.0f}% of range")
    if swing:
        print(f"  20-day swing low: ${swing}")
