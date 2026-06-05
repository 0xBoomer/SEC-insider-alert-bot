"""
edgar_poller.py — Step 1: Pull Form 4 filings from EDGAR and extract transactions.

Flow:
  1. Download EDGAR's quarterly full-index file (form.idx) for the relevant quarter(s).
     This file contains one row per filing with the CORRECT issuer CIK and accession
     number — no guesswork needed.
  2. Filter rows to Form 4, filed within the date range.
  3. For each matching filing, fetch the directory listing to find the XML filename.
  4. Parse the Form 4 XML and extract open-market purchase transactions (code "P").

Why full-index instead of EFTS search API:
  The EFTS API's "ciks" array doesn't reliably include the issuer CIK when a
  third-party filing agent is used. The form.idx file always has the correct CIK.

SEC rate-limit policy: max 10 requests/second; we sleep 0.12s between calls.
Required User-Agent: "name email" per SEC fair-access policy.
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
EDGAR_BASE = "https://www.sec.gov"
FULL_INDEX_BASE = "https://www.sec.gov/Archives/edgar/full-index"

# Change this to your name/email — SEC policy requires a real User-Agent.
HEADERS = {"User-Agent": "InsiderAlertBot contact@example.com"}

# 0.12 s keeps us well under the 10 req/s ceiling
REQUEST_DELAY = 0.12



# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(url, params=None, retries=3, stream=False):
    """GET with exponential back-off. Raises on final failure.
    404s are not retried — they are definitive."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=60, stream=stream)
            resp.raise_for_status()
            if not stream:
                time.sleep(REQUEST_DELAY)
            return resp
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 400, 403):
                raise
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            logger.warning(f"Request failed (attempt {attempt + 1}): {e}. Retrying in {wait}s…")
            time.sleep(wait)
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            logger.warning(f"Request failed (attempt {attempt + 1}): {e}. Retrying in {wait}s…")
            time.sleep(wait)


# ── Quarter helpers ───────────────────────────────────────────────────────────

def _quarter(d):
    return (d.month - 1) // 3 + 1


def _index_url(year, qtr):
    """URL for the quarterly form.idx file."""
    return f"{FULL_INDEX_BASE}/{year}/QTR{qtr}/form.idx"


def _date_quarters(start_date, end_date):
    """Return list of (year, qtr) tuples covering the date range."""
    quarters = []
    d = start_date.replace(day=1)
    while d <= end_date:
        q = (d.year, _quarter(d))
        if q not in quarters:
            quarters.append(q)
        # Advance to next quarter
        if d.month >= 10:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=((d.month - 1) // 3 + 1) * 3 + 1)
    return quarters


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_form4_filings(days_back=1, start_date=None, end_date=None):
    """
    Return a list of transaction dicts for every open-market purchase (code P)
    in the given date range.

    Pass explicit start_date/end_date (date objects) for historical runs.
    Otherwise uses today minus days_back.

    Each dict contains:
        ticker, issuer_name, issuer_cik, filer_name, officer_title,
        is_director, is_officer, shares_purchased, price_per_share,
        total_value, shares_before, shares_after, transaction_date,
        filing_date, accession_no
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=days_back)

    logger.info(f"Scanning EDGAR Form 4 filings from {start_date} to {end_date}")

    # Get all (year, quarter) pairs that overlap with our date range
    quarters = _date_quarters(start_date, end_date)

    # Collect matching filing rows from the index
    filing_rows = []
    for year, qtr in quarters:
        rows = _fetch_index_rows(year, qtr, start_date, end_date)
        filing_rows.extend(rows)
        logger.info(f"QTR{qtr} {year}: {len(rows)} Form 4 filings in date range")

    if not filing_rows:
        logger.info("No Form 4 filings found in date range")
        return []

    # Group by issuer CIK and prioritize cluster candidates (2+ filings from
    # the same issuer today) so they are always processed even if the run is
    # interrupted. Solo-filer issuers follow.
    from collections import defaultdict
    by_cik = defaultdict(list)
    for row in filing_rows:
        by_cik[row["cik"]].append(row)

    cluster_rows = []
    solo_rows = []
    for rows in by_cik.values():
        if len(rows) >= 2:
            cluster_rows.extend(rows)
        else:
            solo_rows.extend(rows)

    ordered_rows = cluster_rows + solo_rows
    n_cluster_issuers = sum(1 for rows in by_cik.values() if len(rows) >= 2)

    logger.info(
        f"Parsing {len(ordered_rows)} Form 4 filings "
        f"({len(cluster_rows)} from {n_cluster_issuers} cluster issuers first, "
        f"then {len(solo_rows)} solo)…"
    )

    transactions = []
    for i, row in enumerate(ordered_rows):
        if i > 0 and i % 500 == 0:
            print(f"  [{i}/{len(ordered_rows)}] parsed… {len(transactions)} open-market purchases so far", flush=True)
        filing_transactions = _parse_form4(row["cik"], row["accession_no"])
        if filing_transactions:
            transactions.extend(filing_transactions)

    logger.info(f"Extracted {len(transactions)} open-market purchase transactions")
    return transactions


def _fetch_index_rows(year, qtr, start_date, end_date):
    """
    Download the quarterly form.idx and return rows for Form 4 filings
    within [start_date, end_date].

    form.idx uses fixed-width columns but spacing varies slightly, so we use
    regex to extract the date and filename reliably.

    Each row looks like:
        4                Company Name          1234567     2026-05-23  edgar/data/1234567/0001234567-26-000001.txt
    """
    import re

    url = _index_url(year, qtr)
    try:
        resp = _get(url)
        lines = resp.text.splitlines()
    except Exception as e:
        logger.error(f"Could not fetch form.idx for {year} QTR{qtr}: {e}")
        return []

    # Pattern: date YYYY-MM-DD followed by spaces then the edgar/data/... path
    row_pattern = re.compile(
        r'^4\s+'           # Form type "4" at start of line
        r'.+?'             # Company name (non-greedy)
        r'(\d+)\s+'        # CIK (digits)
        r'(\d{4}-\d{2}-\d{2})\s+'  # Date filed
        r'(edgar/data/\S+)'         # Filename
    )

    rows = []
    for line in lines:
        m = row_pattern.match(line)
        if not m:
            continue

        cik_raw, date_filed_str, filename = m.group(1), m.group(2), m.group(3)

        try:
            file_date = date.fromisoformat(date_filed_str)
        except ValueError:
            continue

        if not (start_date <= file_date <= end_date):
            continue

        cik = cik_raw.lstrip("0")

        # Accession number is the filename without the .txt extension
        acc_file = filename.split("/")[-1].replace(".txt", "")
        accession_no = _normalise_accession(acc_file)

        rows.append({
            "cik": cik,
            "accession_no": accession_no,
            "file_date": file_date.isoformat(),
        })

    return rows


def _normalise_accession(adsh):
    """
    Return a dashed accession number regardless of input format.
    "000123456724000001" → "0001234567-24-000001"
    "0001234567-24-000001" → "0001234567-24-000001"
    """
    clean = adsh.replace("-", "")
    if len(clean) == 18:
        return f"{clean[:10]}-{clean[10:12]}-{clean[12:]}"
    return adsh


# ── XML parsing helpers ──────────────────────────────────────────────────────

def _parse_form4(cik, accession_no):
    """
    Fetch the Form 4 XML for a given CIK + accession number and return
    a list of open-market purchase transaction dicts.
    Returns [] if found but no qualifying transactions; None if not found.
    """
    acc_nodashes = accession_no.replace("-", "")

    # Get the directory listing to find the XML filename
    dir_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{acc_nodashes}/"

    try:
        resp = _get(dir_url)
        # Parse XML filenames from the HTML directory listing
        xml_filename = _find_xml_in_directory(resp.text, acc_nodashes)
    except Exception as e:
        logger.debug(f"Could not fetch directory for {accession_no} CIK {cik}: {e}")
        return None

    if not xml_filename:
        logger.debug(f"No XML found in {accession_no}")
        return []

    xml_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{acc_nodashes}/{xml_filename}"

    try:
        resp = _get(xml_url)
        root = ET.fromstring(resp.content)
    except Exception as e:
        logger.debug(f"Could not parse XML for {accession_no}: {e}")
        return []

    return _extract_transactions(root, cik, accession_no)


def _find_xml_in_directory(html, acc_nodashes):
    """
    Extract the Form 4 XML filename from an EDGAR directory listing HTML page.
    Looks for .xml files, preferring those named like the accession number.
    """
    import re
    # Find all href links to .xml files in the directory
    matches = re.findall(r'href="([^"]*\.xml)"', html, re.IGNORECASE)
    if not matches:
        return None

    # Prefer a file named after the accession number or containing "form4"
    for m in matches:
        fname = m.split("/")[-1]
        if acc_nodashes.lower() in fname.lower() or "form4" in fname.lower():
            return fname

    # Exclude index XML files, return first remaining
    for m in matches:
        fname = m.split("/")[-1]
        if not fname.endswith("-index.xml") and "xsl" not in fname.lower():
            return fname

    return None


def _get_val(element, path):
    """
    Navigate an XML path and return the text of a nested <value> element,
    or the element's own text, or None.
    """
    node = element.find(path)
    if node is None:
        return None
    val_node = node.find("value")
    if val_node is not None and val_node.text:
        return val_node.text.strip()
    if node.text and node.text.strip():
        return node.text.strip()
    return None


def _extract_transactions(root, cik, accession_no):
    """
    Walk every nonDerivativeTransaction in the Form 4 XML.
    Return one dict per open-market purchase (transactionCode = "P", acquired = "A").
    """
    ticker = (_get_val(root, "issuer/issuerTradingSymbol") or "").upper().strip()
    issuer_name = _get_val(root, "issuer/issuerName") or ""
    issuer_cik = (_get_val(root, "issuer/issuerCik") or cik).lstrip("0")

    if not ticker:
        return []

    filer_name = _get_val(root, "reportingOwner/reportingOwnerId/rptOwnerName") or "Unknown"

    rel = root.find("reportingOwner/reportingOwnerRelationship")
    is_director = rel is not None and _get_val(rel, "isDirector") == "1"
    is_officer = rel is not None and _get_val(rel, "isOfficer") == "1"
    officer_title = (rel is not None and _get_val(rel, "officerTitle") or "")

    if not is_director and not is_officer:
        return []

    transactions = []

    for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _get_val(txn, "transactionCoding/transactionCode")
        if code != "P":
            continue

        acq_disp = _get_val(txn, "transactionAmounts/transactionAcquiredDisposedCode")
        if acq_disp != "A":
            continue

        try:
            shares_raw = _get_val(txn, "transactionAmounts/transactionShares")
            price_raw = _get_val(txn, "transactionAmounts/transactionPricePerShare")
            shares_after_raw = _get_val(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction")
            txn_date = _get_val(txn, "transactionDate") or date.today().isoformat()

            shares = float(shares_raw) if shares_raw else 0.0
            price = float(price_raw) if price_raw else 0.0
            shares_after = float(shares_after_raw) if shares_after_raw else 0.0

            if shares <= 0 or price <= 0:
                continue

            total_value = shares * price
            shares_before = max(0.0, shares_after - shares)

            transactions.append({
                "ticker": ticker,
                "issuer_name": issuer_name,
                "issuer_cik": issuer_cik,
                "filer_name": filer_name,
                "officer_title": officer_title,
                "is_director": is_director,
                "is_officer": is_officer,
                "shares_purchased": shares,
                "price_per_share": price,
                "total_value": total_value,
                "shares_before": shares_before,
                "shares_after": shares_after,
                "transaction_date": txn_date,
                "filing_date": date.today().isoformat(),
                "accession_no": accession_no,
            })

        except (ValueError, TypeError) as e:
            logger.debug(f"Skipping malformed transaction in {accession_no}: {e}")
            continue

    return transactions


# ── Insider purchase history (used for "largest ever" context) ───────────────

def get_largest_prior_purchase(issuer_cik, filer_name, current_value, current_accession=None):
    """
    Check this insider's Form 4 history for the same issuer and return
    (is_largest_ever, prior_max_value, prior_max_year).

    Uses reporterNames from the submissions JSON to pre-filter without fetching
    XMLs where possible, then fetches up to 30 historical XMLs to get amounts.
    Falls back to (False, None, None) on any network error.
    """
    MAX_HISTORY_XMLS = 30

    cik_padded = issuer_cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"

    try:
        resp = _get(url)
        data = resp.json()
    except Exception:
        return (False, None, None)

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    reporter_names_list = recent.get("reporterNames", [])

    filer_lower = filer_name.lower()

    candidates = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        if i >= len(accessions):
            continue

        acc = _normalise_accession(accessions[i].replace("-", ""))
        if acc == current_accession:
            continue

        # Use reporterNames to skip unrelated filings when the field is present
        if reporter_names_list and i < len(reporter_names_list):
            names = reporter_names_list[i]
            if isinstance(names, list):
                if not any(filer_lower in n.lower() for n in names):
                    continue
            elif isinstance(names, str):
                if filer_lower not in names.lower():
                    continue

        date_str = filing_dates[i] if i < len(filing_dates) else ""
        candidates.append((acc, date_str))

    if not candidates:
        return (True, None, None)  # No prior filings — first purchase on record

    prior_purchases = []
    for acc, date_str in candidates[:MAX_HISTORY_XMLS]:
        try:
            txns = _parse_form4(issuer_cik, acc)
            if txns:
                for t in txns:
                    if filer_lower in t["filer_name"].lower() and t["total_value"] > 0:
                        prior_purchases.append((t["total_value"], date_str[:4]))
        except Exception:
            continue

    if not prior_purchases:
        return (True, None, None)

    prior_max_value, prior_max_year = max(prior_purchases, key=lambda x: x[0])
    return (current_value > prior_max_value, prior_max_value, prior_max_year)


# ── 30-day cluster lookback (used by filter_layer) ───────────────────────────

def fetch_recent_form4_accessions(issuer_cik, days=30):
    """
    Return a list of (accession_no, filing_date) tuples for all Form 4 filings
    against this issuer in the last `days` days, using the EDGAR submissions API.
    """
    cik_padded = issuer_cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    try:
        resp = _get(url)
        data = resp.json()
    except Exception as e:
        logger.warning(f"Could not fetch submissions for CIK {issuer_cik}: {e}")
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])

    result = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        if i >= len(filing_dates) or i >= len(accessions):
            continue
        if filing_dates[i] >= cutoff:
            result.append((accessions[i], filing_dates[i]))

    return result
