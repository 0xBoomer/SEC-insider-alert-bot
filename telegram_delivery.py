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
import yfinance as yf

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


def send_summary(alert_count, skip_count, error_count, near_misses=None, score_near_misses=None, solo_near_misses=None, notable_cluster_distress=None):
    """
    Send a brief end-of-run summary so you know the bot fired even on quiet days.
    near_misses: list of (ticker, z_score, cluster_size, cap_m) — failed Z (1.50–1.81)
    score_near_misses: list of (ticker, score, cluster_size, cap_m) — passed survival, score < 45
    solo_near_misses: list of (ticker, name, role, value, price, cap_m) — large single-insider buys
    notable_cluster_distress: list of (ticker, cluster_size, z_score, cap_m) — 5+ insiders, Z < 1.50
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return False

    total = alert_count + skip_count + error_count
    if alert_count == 0:
        msg = (
            f"📋 <b>SEC Insider Bot — Run Complete</b>\n"
            f"No alerts today. Scanned {total} candidate(s): "
            f"{skip_count} filtered/skipped, {error_count} data error(s)."
        )
    else:
        msg = (
            f"📋 <b>SEC Insider Bot — Run Complete</b>\n"
            f"Sent <b>{alert_count}</b> alert(s). "
            f"{skip_count} filtered out, {error_count} data error(s)."
        )

    if notable_cluster_distress:
        msg += "\n\n⚡ <b>Notable cluster despite distress (5+ insiders, Z &lt; 1.50):</b>"
        for ticker, cluster, z, cap_m in sorted(notable_cluster_distress, key=lambda x: -x[1]):
            msg += f"\n  • ${ticker} — {cluster} insiders, Z={z:.2f}, ${cap_m:.1f}M cap"

    if solo_near_misses:
        msg += "\n\n👤 <b>Large solo insider buys ≥$250K (no cluster — check manually):</b>"
        for ticker, name, role, value, price, cap_m in sorted(solo_near_misses, key=lambda x: -x[3]):
            msg += f"\n  • ${ticker} — {name} ({role}): ${value:,.0f} @ ${price:.2f} | ${cap_m:.1f}M cap"

    if score_near_misses:
        msg += "\n\n📉 <b>Below score threshold (passed all checks, score &lt; 45):</b>"
        for ticker, score, cluster, cap_m in sorted(score_near_misses, key=lambda x: -x[1]):
            msg += f"\n  • ${ticker} — {score}/80, {cluster} insiders, ${cap_m:.1f}M cap"

    if near_misses:
        msg += "\n\n⚠️ <b>Near-misses (Z 1.50–1.81 — check manually):</b>"
        for ticker, z, cluster, cap_m in near_misses:
            msg += f"\n  • ${ticker} — Z={z:.2f}, {cluster} insiders, ${cap_m:.1f}M cap"

    url = TELEGRAM_API.format(token=token)
    try:
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        logger.warning(f"Could not send summary: {e}")


def send_sale_alert(ticker, ticker_data):
    """Send a Telegram alert for a notable insider sale cluster or large solo sale."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    message = _format_sale_message(ticker, ticker_data)
    url = TELEGRAM_API.format(token=token)
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        resp.raise_for_status()
        if resp.json().get("ok"):
            logger.info(f"[{ticker}] Sale alert sent")
            return True
        logger.error(f"[{ticker}] Telegram API error: {resp.json()}")
        return False
    except requests.RequestException as e:
        logger.error(f"[{ticker}] Failed to send sale alert: {e}")
        return False


def _format_sale_message(ticker, ticker_data):
    market_cap_m = ticker_data["market_cap"] / 1_000_000
    transactions = ticker_data["transactions"]
    issuer_name = ticker_data.get("issuer_name", ticker)
    cluster_size = ticker_data["cluster_size"]

    # Price momentum context
    momentum_str = ""
    try:
        hist = yf.Ticker(ticker).history(period="6mo")
        if len(hist) >= 2:
            current = float(hist["Close"].iloc[-1])
            mo1 = float(hist["Close"].iloc[-21]) if len(hist) >= 21 else None
            mo3 = float(hist["Close"].iloc[-63]) if len(hist) >= 63 else None
            mo6 = float(hist["Close"].iloc[0])
            parts = []
            if mo3:
                chg3 = (current - mo3) / mo3 * 100
                parts.append(f"{chg3:+.0f}% (3mo)")
            chg6 = (current - mo6) / mo6 * 100
            parts.append(f"{chg6:+.0f}% (6mo)")
            if parts:
                label = "Rip Sell" if (mo3 and chg3 > 20) or chg6 > 30 else "context"
                momentum_str = f"📈 Stock {label}: {', '.join(parts)}"
    except Exception:
        pass

    lines = []
    lines.append("─" * 36)
    label = "SALE CLUSTER" if cluster_size >= 2 else "LARGE SOLO SALE"
    lines.append(f"🔴 <b>INSIDER {label} — ${ticker}</b>")
    lines.append(f"<i>{issuer_name}</i>")
    lines.append(f"Cap: ${market_cap_m:.1f}M  |  {cluster_size} insider(s) selling")
    lines.append("─" * 36)

    for txn in transactions:
        role = txn["officer_title"] or ("Director" if txn["is_director"] else "Insider")
        pos_pct = txn["shares_sold"] / txn["shares_before"] * 100 if txn["shares_before"] > 0 else 100
        lines.append(
            f"  • {txn['filer_name']} ({role}): "
            f"{txn['shares_sold']:,.0f} sh @ ${txn['price_per_share']:.2f} "
            f"= ${txn['total_value']:,.0f}  |  -{pos_pct:.0f}% of position"
        )
        lines.append(f"    Remaining: {txn['shares_after']:,.0f} sh")

    if momentum_str:
        lines.append("")
        lines.append(momentum_str)

    lines.append("─" * 36)
    return "\n".join(lines)


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
    issuer_cik = ticker_data.get("issuer_cik", "")
    for txn in sorted(transactions, key=lambda t: t["total_value"], reverse=True):
        role = txn["officer_title"] or ("Director" if txn["is_director"] else "Insider")
        comp, comp_label = _get_comp_with_label(issuer_cik, txn["filer_name"], txn["officer_title"])
        pct_of_comp = txn["total_value"] / comp * 100

        if txn["shares_before"] > 0:
            pos_increase = txn["shares_purchased"] / txn["shares_before"] * 100
            pos_str = f" | +{pos_increase:.0f}% pos"
        else:
            pos_str = " | new stake"

        if txn.get("is_largest_ever"):
            prior = txn.get("prior_max_purchase")
            year = txn.get("prior_max_year")
            if prior and year:
                largest_str = f" ⭐ largest ever (prev. ${prior:,.0f} in {year})"
            else:
                largest_str = " ⭐ first purchase on record"
        else:
            largest_str = ""

        lines.append(
            f"  • {txn['filer_name']} ({role}): "
            f"{txn['shares_purchased']:,.0f} sh "
            f"@ ${txn['price_per_share']:.2f} "
            f"= ${txn['total_value']:,.0f} "
            f"({pct_of_comp:.0f}% of {comp_label}{pos_str}){largest_str}"
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


def _get_comp_with_label(issuer_cik, filer_name, title):
    """
    Return (comp_value, label_string) for message formatting.
    Label distinguishes real DEF 14A data from estimates.
    """
    from comp_lookup import get_executive_comp
    from filter_layer import estimate_annual_comp

    real = get_executive_comp(issuer_cik, filer_name)
    if real:
        return real, f"${real:,.0f} comp (proxy)"
    est = estimate_annual_comp(title)
    return est, f"~${est:,.0f} comp (est)"
