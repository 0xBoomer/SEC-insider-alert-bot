"""
edgar_poller.py — Step 1: Pull Form 4 filings from EDGAR and extract transactions.

Flow:
  1. Query the EDGAR EFTS full-text search API for Form 4 filings in the last N days.
  2. For each filing, fetch the index JSON to find the XML document filename.
  3. Parse the Form 4 XML and extract open-market purchase transactions (code "P").
  4. Return a list of transaction dicts ready for the filter layer.

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
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

# Change this to your name/email — SEC policy requires a real User-Agent.
HEADERS = {"User-Agent": "InsiderAlertBot contact@example.com"}

# 0.12 s keeps us well under the 10 req/s ceiling
REQUEST_DELAY = 0.12

# Max filings to scan per run (keeps GitHub Actions runtime bounded)
MAX_FILINGS = 2000


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(url, params=None, retries=3):
    """GET with exponential back-off. Raises on final failure."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            logger.warning(f"Request failed (attempt {attempt + 1}): {e}. Retrying in {wait}s…")
            time.sleep(wait)


# ── Public entry point ───────────────────────────────────────────────────────

def fetch_form4_filings(days_back=1):
    """
    Return a list of transaction dicts for every open-market purchase (code P)
    filed in the last `days_back` calendar days.

    Each dict contains:
        ticker, issuer_name, issuer_cik, filer_name, officer_title,
        is_director, is_officer, shares_purchased, price_per_share,
        total_value, shares_before, shares_after, transaction_date,
        filing_date, accession_no
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=days_back)

    logger.info(f"Scanning EDGAR Form 4 filings from {start_date} to {end_date}")

    transactions = []
    offset = 0
    page_size = 40  # EFTS default max per page

    while offset < MAX_FILINGS:
        params = {
            "forms": "4",
            "dateRange": "custom",
            "startdt": start_date.isoformat(),
            "enddt": end_date.isoformat(),
            "from": offset,
            "size": page_size,
        }

        try:
            resp = _get(EFTS_URL, params=params)
            data = resp.json()
        except Exception as e:
            logger.error(f"EFTS search failed at offset {offset}: {e}")
            break

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            source = hit.get("_source", {})
            # EFTS returns accession_no with dashes, e.g. "0001234567-24-000001"
            accession_no = source.get("accession_no") or hit.get("_id", "")
            # entity_id is the filer CIK (zero-padded 10-digit string)
            cik = source.get("entity_id", "").lstrip("0")

            if not accession_no or not cik:
                logger.debug(f"Skipping hit with missing accession/CIK: {source}")
                continue

            filing_transactions = _parse_form4(cik, accession_no)
            transactions.extend(filing_transactions)

        total_available = data.get("hits", {}).get("total", {}).get("value", 0)
        offset += page_size
        if offset >= total_available:
            break

    logger.info(f"Extracted {len(transactions)} open-market purchase transactions")
    return transactions


# ── XML parsing helpers ──────────────────────────────────────────────────────

def _parse_form4(cik, accession_no):
    """
    Fetch the Form 4 XML for a given CIK + accession number and return
    a list of open-market purchase transaction dicts.
    """
    acc_nodashes = accession_no.replace("-", "")

    # The filing index JSON lists every document in the submission package.
    index_url = (
        f"{EDGAR_BASE}/Archives/edgar/data/{cik}/"
        f"{acc_nodashes}/{acc_nodashes}-index.json"
    )

    try:
        resp = _get(index_url)
        index_data = resp.json()
    except Exception as e:
        logger.debug(f"Could not fetch index for {accession_no}: {e}")
        return []

    xml_filename = _find_xml_filename(index_data)
    if not xml_filename:
        logger.debug(f"No XML found in filing {accession_no}")
        return []

    xml_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{acc_nodashes}/{xml_filename}"

    try:
        resp = _get(xml_url)
        root = ET.fromstring(resp.content)
    except Exception as e:
        logger.debug(f"Could not parse XML for {accession_no}: {e}")
        return []

    return _extract_transactions(root, cik, accession_no)


def _find_xml_filename(index_data):
    """
    Scan the filing index for the Form 4 XML document.
    Prefer documents explicitly typed "4"; fall back to any .xml file.
    """
    docs = index_data.get("documents", [])

    # First pass: look for the primary Form 4 document
    for doc in docs:
        if doc.get("type") == "4" and doc.get("filename", "").endswith(".xml"):
            return doc["filename"]

    # Second pass: any XML (some filings use non-standard type labels)
    for doc in docs:
        fname = doc.get("filename", "")
        if fname.endswith(".xml") and not fname.endswith("-index.xml"):
            return fname

    return None


def _get_val(element, path):
    """
    Navigate an XML path like "transactionAmounts/transactionShares" and return
    the text of a nested <value> element, or the element's own text, or None.

    Form 4 XML wraps most numeric fields as:
        <transactionShares><value>10000</value><footnoteId id="F1"/></transactionShares>
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
    Walk every nonDerivativeTransaction in the Form 4 XML tree.
    Return one dict per open-market purchase (transactionCode = "P", acquired = "A").
    """
    # ── Issuer info ──────────────────────────────────────────────────────────
    ticker = (_get_val(root, "issuer/issuerTradingSymbol") or "").upper().strip()
    issuer_name = _get_val(root, "issuer/issuerName") or ""
    issuer_cik = (_get_val(root, "issuer/issuerCik") or cik).lstrip("0")

    if not ticker:
        return []  # Can't do anything without a ticker symbol

    # ── Reporting owner info ─────────────────────────────────────────────────
    filer_name = _get_val(root, "reportingOwner/reportingOwnerId/rptOwnerName") or "Unknown"

    rel = root.find("reportingOwner/reportingOwnerRelationship")
    is_director = rel is not None and _get_val(rel, "isDirector") == "1"
    is_officer = rel is not None and _get_val(rel, "isOfficer") == "1"
    officer_title = (rel is not None and _get_val(rel, "officerTitle") or "")

    # Skip if not a director or officer (we only care about insiders)
    if not is_director and not is_officer:
        return []

    # ── Transaction rows ─────────────────────────────────────────────────────
    transactions = []

    for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _get_val(txn, "transactionCoding/transactionCode")
        if code != "P":
            continue  # Only open-market purchases

        # Acquired/Disposed flag — "A" = buy, "D" = sell
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

            # Skip zero-value rows (data errors / footnote-only rows)
            if shares <= 0 or price <= 0:
                continue

            total_value = shares * price
            # Shares owned before this purchase
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
            logger.debug(f"Skipping malformed transaction row in {accession_no}: {e}")
            continue

    return transactions


# ── 30-day cluster lookback (used by filter_layer) ───────────────────────────

def fetch_recent_form4_accessions(issuer_cik, days=30):
    """
    Return a list of (accession_no, filing_date) tuples for all Form 4 filings
    by ANY reporter against this issuer in the last `days` days.

    Uses the EDGAR submissions endpoint which returns the company's full
    recent-filing history in one JSON blob — much faster than EFTS pagination
    when we already know the CIK.
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
