# SEC Insider Trading Alert Bot

A free-to-run Python bot that monitors SEC Form 4 filings daily and delivers filtered, scored insider purchase alerts to a Telegram channel.

**Runs free on GitHub Actions (5 days/week, no server needed).**

---

## What It Does

Each weekday at 9 AM ET, the bot:

1. **Polls EDGAR** for all Form 4 filings in the last 24 hours, extracting open-market purchases (code `P`) only.
2. **Applies four hard filters:**
   - Market cap < $500M (small-cap focus — insider edge is strongest here)
   - 2+ distinct insiders buying the same ticker within 30 days
   - Purchase > 5% of estimated annual comp OR increases position by 10%+
   - Not a routine same-quarter purchase vs prior year
3. **Runs a survival check** — rejects companies with Altman Z-score < 1.81 or current ratio < 1.0.
4. **Scores survivors 0–80:**
   - Purchase Quality (0–30): value vs compensation + position increase
   - Cluster Strength (0–20, +5 bonus for buys within 7 days)
   - Price Context (0–15): buying near 52-week low is more bullish
   - Earnings Proximity (0–15): freshness of the signal relative to last earnings
5. **Sends a Telegram alert** for any ticker scoring ≥ 45, including a suggested entry note based on the 20-day swing low.

---

## Setup

### 1. Fork/Clone This Repository

```bash
git clone https://github.com/YOUR_USERNAME/sec-insider-alert-bot.git
cd sec-insider-alert-bot
```

### 2. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts to create a bot.
3. BotFather will give you a **bot token** that looks like: `7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
4. Save this token — you'll need it as a secret.

### 3. Get Your Telegram Chat ID

**For a personal chat:**
1. Message your new bot once (just say "hi").
2. Visit `https://api.telegram.org/bot{YOUR_TOKEN}/getUpdates` in a browser.
3. Find `"chat":{"id": XXXXXXXX}` in the response — that number is your chat ID.

**For a channel/group:**
1. Add your bot to the channel/group as an admin.
2. Send a message in the channel.
3. Use the same `getUpdates` URL to find the chat ID (will be negative for groups, like `-100xxxxxxxxxx`).

### 4. Add GitHub Secrets

In your GitHub repository:
1. Go to **Settings → Secrets and variables → Actions → New repository secret**
2. Add two secrets:
   - `TELEGRAM_BOT_TOKEN` — your bot token from BotFather
   - `TELEGRAM_CHAT_ID` — your chat ID from step 3

### 5. Update the User-Agent Header

SEC EDGAR requires a descriptive User-Agent per their [fair-access policy](https://www.sec.gov/os/accessing-edgar-data).

Open `edgar_poller.py` and update line 26:
```python
HEADERS = {"User-Agent": "YourBotName your@email.com"}
```

### 6. Enable GitHub Actions

The workflow file is at `.github/workflows/run.yml`. It runs automatically Monday–Friday at 9 AM ET once you push to `main`.

To **test immediately**, go to **Actions → SEC Insider Alert Bot → Run workflow**.

---

## Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export TELEGRAM_BOT_TOKEN="your_token_here"
export TELEGRAM_CHAT_ID="your_chat_id_here"

# Run the bot
python main.py
```

The bot will log everything to stdout. On a quiet day you'll still get a summary message in Telegram.

---

## Adjusting Thresholds

Every threshold lives in the relevant module as a named constant at the top of the file:

| File | Constant | Default | Purpose |
|------|----------|---------|---------|
| `filter_layer.py` | `MAX_MARKET_CAP` | `500_000_000` | Max market cap to consider |
| `filter_layer.py` | `MIN_CLUSTER_SIZE` | `2` | Min unique insiders required |
| `filter_layer.py` | `MIN_VALUE_PCT_OF_COMP` | `0.05` | Min purchase as % of est. comp |
| `filter_layer.py` | `MIN_POSITION_INCREASE_PCT` | `0.10` | Min position increase |
| `filter_layer.py` | `LOOKBACK_DAYS` | `30` | Cluster lookback window |
| `survival_check.py` | `MIN_ALTMAN_Z` | `1.81` | Min Z-score |
| `survival_check.py` | `MIN_CURRENT_RATIO` | `1.0` | Min current ratio |
| `scoring_engine.py` | `MIN_SCORE` | `45` | Min score to trigger alert |
| `scoring_engine.py` | `CLUSTER_BONUS_DAYS` | `7` | Window for cluster bonus |

---

## File Structure

```
sec-insider-alert-bot/
├── main.py               # Orchestrator — runs the full pipeline
├── edgar_poller.py       # EDGAR API polling + Form 4 XML parsing
├── filter_layer.py       # Four hard filters
├── survival_check.py     # Altman Z-score, current ratio, debt maturity
├── scoring_engine.py     # 0–80 scoring across four components
├── telegram_delivery.py  # Telegram Bot API message formatting + delivery
├── deduplication.py      # processed.json read/write helpers
├── processed.json        # Tracks seen accession numbers (auto-updated)
├── requirements.txt
└── .github/
    └── workflows/
        └── run.yml       # GitHub Actions schedule
```

---

## Alert Message Format

```
────────────────────────────────────
🚨 INSIDER ALERT — $ACME
Acme Corp Inc
Score: 67/80  |  Cap: $142M
  PQ=22 CS=14 (+5 cluster bonus) PC=12 EP=14
────────────────────────────────────
📦 Cluster: 3 insider(s) buying
  • Jane Smith (CEO): 50,000 sh @ $4.20 = $210,000 (42% of ~$500,000 comp | new stake)
  • Bob Jones (Director): 25,000 sh @ $4.18 = $104,500 (105% of ~$100,000 comp | +18% pos)

📊 Survival: Z=2.34 (grey) | CR=1.82 | ✅ Debt OK

📈 Price: $4.22  |  52W Low $3.10  |  52W High $9.80
   → In bottom 10% of 52-week range

📍 Suggested entry: near 20-day swing low of $3.95
   (price is at/near swing low — entry zone active)
────────────────────────────────────
```

---

## Limitations & Notes

- **Compensation estimates are rough.** Without a paid proxy-filing API, comp is estimated by title keyword (CEO ~$500K, Director ~$100K, etc.). The actual conviction signal matters more than the exact %.
- **Altman Z-score was designed for manufacturing firms.** For tech/biotech/financial companies, treat the distress threshold with more judgment — it's a useful filter, not gospel.
- **The routine-purchase check is best-effort.** EDGAR submissions don't always include reporter names in the JSON metadata, so same-quarter prior-year filtering may miss some plan-driven buys.
- **This is not investment advice.** Insider purchases are one signal among many. Always do your own research.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `requests` | HTTP calls to EDGAR and Telegram APIs |
| `yfinance` | Market cap, price history, financials, earnings dates |
| `pandas` | DataFrame handling for yfinance financial statements |
| `numpy` | Numerical operations |

No paid APIs. No database. No Docker. Runs entirely on GitHub Actions free tier.
