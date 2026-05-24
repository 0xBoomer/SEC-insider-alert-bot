"""
telegram_delivery.py — Step 5: Format and send Telegram alerts.

Uses the Telegram Bot API directly via requests — no python-telegram-bot library needed.
Endpoint: POST https://api.telegram.org/bot{TOKEN}/sendMessage

Message format (plain text with emoji for readability):
  ─────────────────────────────
  🚨 INSIDER ALERT — $TICKER
  Score: 67/80  |  Cap: $142M
  ─────────────────────────────
  📦 Cluster: 3 insiders bought
     • Jane Smith (CEO): 50,000 sh @ $4.20 = $210K (42% of ~$500K comp)
     • Bob Jones (Director): 25,000 sh @ $4.18 = $104.5K (+15% position)
     • ...
  📊 Survival: Z-score 2.3 | CR 1.8 | ⚠️ Debt flag
  📈 Price: $4.22 | 52W Low $3.10 | 52W High $9.80
       → Near 52-week low (bottom 15% of range)
  🗓 Earnings: 8 days ago
  📍 Suggested entry: near 20-day swing low of $3.95
  ─────────────────────────────

Secrets are read from environment variables:
  TELEGRAM_BOT_TOKEN  — from BotFather
  TELEGRAM_CHAT_ID    — channel or group chat ID
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_alert(ticker, ticker_data, score_result, survival_metrics):
    """
    Build and send a Telegram message for one qualifying ticker.
    Returns True on success, False on failure.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in environment")
        return False

    message = _format_message(ticker, ticker_data, score_result, survival_metrics)

    url = TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",  # Use HTML for bold/monospace
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=20)
        resp.raise_for_status()
        result = resp.json()
        if result.get("ok"):
            logger.info(f"[{ticker}] Telegram alert sent")
            return True
        else:
            logger.error(f"[{ticker}] Telegram API error: {result}")
            return False
    except requests.RequestException as e:
        logger.error(f"[{ticker}] Failed to send Telegram alert: {e}")
        return False


def send_summary(alert_count, skip_count, error_count):
    """
    Send a brief end-of-run summary so you know the bot fired even on quiet days.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return False

    if alert_count == 0:
        msg = (
            f"📋 <b>SEC Insider Bot — Daily Run Complete</b>\n"
            f"No alerts today.  Scanned {alert_count + skip_count + error_count} "
            f"candidate(s): {skip_count} filtered/skipped, {error_count} data error(s)."
        )
    else:
        msg = (
            f"📋 <b>SEC Insider Bot — Daily Run Complete</b>\n"
            f"Sent <b>{alert_count}</b> alert(s).  "
            f"{skip_count} filtered out, {error_count} data error(s)."
        )

    url = TELEGRAM_API.format(token=token)
    try:
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        logger.warning(f"Could not send summary: {e}")


# ── Message formatting ────────────────────────────────────────────────────────

def _format_message(ticker, ticker_data, score_result, survival_metrics):
    market_cap_m = ticker_data["market_cap"] / 1_000_000
    transactions = ticker_data["transactions"]
    issuer_name = ticker_data.get("issuer_name", ticker)
    total_score = score_result["total"]
    cluster_size = ticker_data["cluster_size"]

    lines = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append("─" * 36)
    lines.append(f"🚨 <b>INSIDER ALERT — ${ticker}</b>")
    lines.append(f"<i>{issuer_name}</i>")
    lines.append(
        f"Score: <b>{total_score}/80</b>  |  Cap: ${market_cap_m:.1f}M"
    )

    # Score breakdown
    bonus_str = " (+5 cluster bonus)" if score_result["cluster_bonus"] else ""
    lines.append(
        f"  PQ={score_result['purchase_quality']} "
        f"CS={score_result['cluster_strength']}{bonus_str} "
        f"PC={score_result['price_context']} "
        f"EP={score_result['earnings_proximity']}"
    )
    lines.append("─" * 36)

    # ── Cluster summary ───────────────────────────────────────────────────────
    lines.append(f"📦 <b>Cluster: {cluster_size} insider(s) buying</b>")
    for txn in sorted(transactions, key=lambda t: t["total_value"], reverse=True):
        role = txn["officer_title"] or ("Director" if txn["is_director"] else "Insider")
        comp = _comp_label(txn["officer_title"])
        pct_of_comp = txn["total_value"] / comp * 100

        if txn["shares_before"] > 0:
            pos_increase = txn["shares_purchased"] / txn["shares_before"] * 100
            pos_str = f" | +{pos_increase:.0f}% pos"
        else:
            pos_str = " | new stake"

        lines.append(
            f"  • {txn['filer_name']} ({role}): "
            f"{txn['shares_purchased']:,.0f} sh "
            f"@ ${txn['price_per_share']:.2f} "
            f"= ${txn['total_value']:,.0f} "
            f"({pct_of_comp:.0f}% of ~${comp:,.0f} comp{pos_str})"
        )

    # ── Survival metrics ──────────────────────────────────────────────────────
    lines.append("")
    z = survival_metrics["z_score"]
    cr = survival_metrics["current_ratio"]
    debt_flag = "⚠️ Debt flag" if survival_metrics["debt_maturity_flag"] else "✅ Debt OK"

    # Z-score zone label
    if z >= 2.99:
        z_label = "safe"
    elif z >= 1.81:
        z_label = "grey"
    else:
        z_label = "distress"  # Shouldn't reach here — survival check would have filtered

    lines.append(
        f"📊 <b>Survival:</b> Z={z:.2f} ({z_label}) | "
        f"CR={cr:.2f} | {debt_flag}"
    )

    # ── Price context ─────────────────────────────────────────────────────────
    lines.append("")
    cp = score_result.get("current_price")
    low52 = score_result.get("price_52w_low")
    high52 = score_result.get("price_52w_high")
    swing_low = score_result.get("swing_low")

    if cp and low52 and high52:
        rng = high52 - low52
        pos_pct = (cp - low52) / rng * 100 if rng > 0 else 50
        lines.append(
            f"📈 <b>Price:</b> ${cp:.2f}  |  "
            f"52W Low ${low52:.2f}  |  52W High ${high52:.2f}"
        )
        lines.append(
            f"   → In bottom {pos_pct:.0f}% of 52-week range"
            if pos_pct <= 50
            else f"   → In top {100 - pos_pct:.0f}% of 52-week range"
        )

    # ── Entry note ────────────────────────────────────────────────────────────
    if swing_low:
        lines.append("")
        lines.append(f"📍 <b>Suggested entry:</b> near 20-day swing low of ${swing_low:.2f}")
        if cp and cp <= swing_low * 1.03:
            lines.append("   (price is at/near swing low — entry zone active)")
        elif cp and cp > swing_low * 1.10:
            lines.append(f"   (price is {((cp/swing_low)-1)*100:.0f}% above swing low — wait for pullback)")

    lines.append("─" * 36)
    return "\n".join(lines)


def _comp_label(title):
    """Re-use the same comp estimate logic for message formatting."""
    from filter_layer import estimate_annual_comp
    return estimate_annual_comp(title)
