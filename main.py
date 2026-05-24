"""
main.py — Orchestrator for the SEC Insider Trading Alert Bot.

Pipeline:
  1. edgar_poller   — Pull Form 4 open-market purchases from the last 24h
  2. deduplication  — Skip accession numbers already processed
  3. filter_layer   — Apply four hard filters (cap, cluster, conviction, routine)
  4. survival_check — Reject financially distressed companies (Z-score, CR)
  5. scoring_engine — Score survivors 0–80; skip if score < 45
  6. telegram       — Send formatted alert for each qualifying ticker
  7. deduplication  — Save updated processed.json

Run locally:
    export TELEGRAM_BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."
    python main.py

Run via GitHub Actions: see .github/workflows/run.yml
"""

import logging
import sys

import deduplication
import edgar_poller
import filter_layer
import scoring_engine
import survival_check
import telegram_delivery

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")


def run():
    logger.info("=" * 60)
    logger.info("SEC Insider Alert Bot — starting daily run")
    logger.info("=" * 60)

    alert_count = 0
    skip_count = 0
    error_count = 0

    # ── Step 1: Load deduplication state ─────────────────────────────────────
    processed = deduplication.load_processed()
    logger.info(f"Loaded {len(processed)} previously processed accession numbers")

    # ── Step 2: Pull Form 4 filings from last 24h ─────────────────────────────
    logger.info("Polling EDGAR for Form 4 filings (last 24h)…")
    transactions = edgar_poller.fetch_form4_filings(days_back=1)
    logger.info(f"Found {len(transactions)} raw open-market purchase transaction(s)")

    if not transactions:
        logger.info("No transactions found — exiting")
        telegram_delivery.send_summary(0, 0, 0)
        return

    # ── Step 3: Deduplicate ───────────────────────────────────────────────────
    new_transactions = [
        t for t in transactions
        if not deduplication.is_processed(t["accession_no"], processed)
    ]
    logger.info(
        f"{len(new_transactions)} new transaction(s) after deduplication "
        f"({len(transactions) - len(new_transactions)} already processed)"
    )

    # Mark all as processed now (before filtering) so reruns don't re-evaluate
    # the same filings even if they're later filtered out
    new_accessions = {t["accession_no"] for t in new_transactions}
    processed.update(new_accessions)

    if not new_transactions:
        logger.info("No new transactions — exiting")
        deduplication.save_processed(processed)
        telegram_delivery.send_summary(0, 0, 0)
        return

    # ── Step 4: Filter layer ──────────────────────────────────────────────────
    logger.info("Applying filters (market cap, cluster, conviction, routine check)…")
    filtered = filter_layer.apply_filters(new_transactions)
    skip_count += len(set(t["ticker"] for t in new_transactions)) - len(filtered)
    logger.info(f"{len(filtered)} ticker(s) passed all four filters")

    if not filtered:
        logger.info("No tickers passed filters — exiting")
        deduplication.save_processed(processed)
        telegram_delivery.send_summary(0, skip_count, 0)
        return

    # ── Steps 5–7: Survival check + scoring + delivery ────────────────────────
    for ticker, ticker_data in filtered.items():
        logger.info(f"\n{'─'*50}")
        logger.info(f"Processing {ticker}…")

        # Step 5: Survival check
        survival = survival_check.check_survival(ticker, ticker_data["market_cap"])

        if survival is None:
            logger.warning(f"[{ticker}] Skipped — could not fetch financial data")
            error_count += 1
            continue

        if not survival["passes"]:
            logger.info(f"[{ticker}] Rejected by survival check: {survival['reject_reason']}")
            skip_count += 1
            continue

        # Step 6: Scoring
        score_result = scoring_engine.score_ticker(ticker, ticker_data, survival)

        if not score_result["passes"]:
            logger.info(
                f"[{ticker}] Below score threshold "
                f"({score_result['total']}/80 < {scoring_engine.MIN_SCORE})"
            )
            skip_count += 1
            continue

        # Step 7: Send Telegram alert
        success = telegram_delivery.send_alert(ticker, ticker_data, score_result, survival)
        if success:
            alert_count += 1
        else:
            error_count += 1

    # ── Persist updated deduplication state ───────────────────────────────────
    deduplication.save_processed(processed)
    logger.info(f"Saved {len(processed)} accession numbers to processed.json")

    # ── End-of-run summary ────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(
        f"Run complete — {alert_count} alert(s) sent, "
        f"{skip_count} filtered/below-threshold, {error_count} error(s)"
    )
    telegram_delivery.send_summary(alert_count, skip_count, error_count)


if __name__ == "__main__":
    run()
