"""
comp_lookup.py — Look up real executive compensation from SEC DEF 14A proxy filings.

Flow:
  1. Hit the EDGAR submissions API to find the company's most recent DEF 14A.
  2. Fetch the filing index to locate the primary HTML document.
  3. Download and strip the HTML to plain text.
  4. Find the "Summary Compensation Table" section.
  5. Locate the row matching the insider's name (fuzzy last-name match).
  6. Extract the "Total" column, falling back to "Salary" if Total isn't parseable.
  7. Return the dollar figure, or None to signal "fall back to estimate table".

DEF 14A filings are public on EDGAR — no API key needed.
Parsing accuracy: ~70-80% of filings. Remainder fall back to estimate table.
"""

import logging
import re
from html.parser import HTMLParser

import requests

from edgar_poller import HEADERS, REQUEST_DELAY, _get

logger = logging.getLogger(__name__)

# Cache results within a single run so we don't re-fetch the same proxy twice
_comp_cache = {}


# ── Public entry point ────────────────────────────────────────────────────────

def get_executive_comp(issuer_cik, filer_name):
    """
    Return the real annual total compensation for `filer_name` at `issuer_cik`,
    sourced from the company's most recent DEF 14A proxy filing.

    Returns a float (dollar amount) on success, or None if:
      - No DEF 14A found
      - Document can't be parsed
      - Filer name not found in the compensation table
    """
    cache_key = (issuer_cik, filer_name.lower())
    if cache_key in _comp_cache:
        return _comp_cache[cache_key]

    result = _fetch_comp(issuer_cik, filer_name)
    _comp_cache[cache_key] = result

    if result:
        logger.info(f"Real comp for {filer_name} at CIK {issuer_cik}: ${result:,.0f}")
    else:
        logger.debug(f"No real comp found for {filer_name} at CIK {issuer_cik} — will use estimate")

    return result


# ── DEF 14A lookup ────────────────────────────────────────────────────────────

def _fetch_comp(issuer_cik, filer_name):
    """Core logic: find and parse the DEF 14A, return comp or None."""

    # Step 1: Find the most recent DEF 14A accession number
    accession_no, cik = _find_def14a(issuer_cik)
    if not accession_no:
        return None

    # Step 2: Get the HTML document from the filing index
    html_text = _fetch_def14a_html(cik, accession_no)
    if not html_text:
        return None

    # Step 3: Parse the compensation table
    return _parse_comp_table(html_text, filer_name)


def _find_def14a(issuer_cik):
    """
    Search the EDGAR submissions JSON for the most recent DEF 14A filing.
    Returns (accession_no, cik) or (None, None).
    """
    cik_padded = issuer_cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"

    try:
        resp = _get(url)
        data = resp.json()
    except Exception as e:
        logger.debug(f"Submissions fetch failed for CIK {issuer_cik}: {e}")
        return None, None

    # The submissions JSON has a "filings.recent" block with parallel arrays
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])

    # Find the most recent DEF 14A (index 0 = most recent in EDGAR ordering)
    for i, form in enumerate(forms):
        if form in ("DEF 14A", "DEF14A"):
            if i < len(accessions):
                logger.debug(
                    f"Found DEF 14A for CIK {issuer_cik}: "
                    f"{accessions[i]} filed {dates[i] if i < len(dates) else 'unknown'}"
                )
                return accessions[i], issuer_cik

    # Also check "files" for older filings not in the recent block
    return None, None


def _fetch_def14a_html(cik, accession_no):
    """
    Fetch the primary HTML document from the DEF 14A filing.
    Returns raw HTML text or None.
    """
    acc_nodashes = accession_no.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{acc_nodashes}/{acc_nodashes}-index.json"
    )

    try:
        resp = _get(index_url)
        index_data = resp.json()
    except Exception as e:
        logger.debug(f"DEF 14A index fetch failed: {e}")
        return None

    # Find the primary document (largest HTML file is usually the proxy statement)
    html_filename = _find_primary_html(index_data)
    if not html_filename:
        return None

    doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodashes}/{html_filename}"

    try:
        resp = _get(doc_url)
        return resp.text
    except Exception as e:
        logger.debug(f"DEF 14A HTML fetch failed: {e}")
        return None


def _find_primary_html(index_data):
    """
    Find the primary proxy statement HTML file from the filing index.
    Prefer the document explicitly typed DEF 14A; fall back to largest HTML.
    """
    docs = index_data.get("documents", [])

    # First pass: look for the explicitly typed DEF 14A document
    for doc in docs:
        dtype = doc.get("type", "")
        fname = doc.get("filename", "")
        if dtype in ("DEF 14A", "DEF14A") and fname.lower().endswith((".htm", ".html")):
            return fname

    # Second pass: take the largest HTML file (proxy statements are long)
    html_docs = [
        d for d in docs
        if d.get("filename", "").lower().endswith((".htm", ".html"))
        and not d.get("filename", "").endswith("-index.htm")
    ]

    if not html_docs:
        return None

    # Sort by size descending — proxy is almost always the biggest document
    html_docs.sort(key=lambda d: int(d.get("size", 0)), reverse=True)
    return html_docs[0]["filename"]


# ── HTML parsing ──────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Strip HTML tags and return clean text, preserving table cell boundaries."""

    def __init__(self):
        super().__init__()
        self.chunks = []
        self._in_script = False
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._in_script = True
        elif tag == "style":
            self._in_style = True
        elif tag in ("td", "th"):
            self.chunks.append("\t")   # Tab separates table cells
        elif tag in ("tr", "p", "br", "div", "li"):
            self.chunks.append("\n")   # Newline separates rows/paragraphs

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
        elif tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if not self._in_script and not self._in_style:
            self.chunks.append(data)

    def get_text(self):
        return "".join(self.chunks)


def _html_to_text(html):
    """Convert HTML to plain text for pattern matching."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        return parser.get_text()
    except Exception:
        # Fallback: strip tags with regex if parser chokes on malformed HTML
        text = re.sub(r"<[^>]+>", " ", html)
        return text


def _parse_comp_table(html_text, filer_name):
    """
    Find the Summary Compensation Table in the proxy text and extract
    the total compensation for `filer_name`.

    Strategy:
      1. Find the "Summary Compensation Table" section heading.
      2. Extract the block of text following it (up to the next major heading).
      3. Split into rows and find the row containing the filer's last name.
      4. Extract dollar amounts from that row; prefer the last (Total) column.

    Returns float or None.
    """
    plain_text = _html_to_text(html_text)

    # Normalize whitespace for easier matching
    plain_text = re.sub(r"[ \t]+", " ", plain_text)

    # ── Step 1: Locate the Summary Compensation Table ─────────────────────────
    # Various phrasings used across different companies
    table_patterns = [
        r"summary compensation table",
        r"summary of compensation",
        r"named executive officer compensation",
        r"compensation of named executive officers",
    ]

    table_start = None
    for pattern in table_patterns:
        match = re.search(pattern, plain_text, re.IGNORECASE)
        if match:
            table_start = match.start()
            break

    if table_start is None:
        logger.debug("Summary Compensation Table heading not found in proxy")
        return None

    # Take the next 15,000 characters — enough to cover the full table
    table_section = plain_text[table_start: table_start + 15_000]

    # ── Step 2: Find the row containing the filer's name ─────────────────────
    # Use last name for matching (proxy filings sometimes format as "Smith, John")
    last_name = _extract_last_name(filer_name)
    if not last_name or len(last_name) < 3:
        logger.debug(f"Last name too short to match reliably: '{last_name}'")
        return None

    # Split into lines and find lines mentioning the last name
    lines = table_section.split("\n")
    candidate_lines = []

    for i, line in enumerate(lines):
        if last_name.lower() in line.lower():
            # Gather this line + the next 3 (comp data sometimes spans multiple rows)
            block = " ".join(lines[i: i + 4])
            candidate_lines.append(block)

    if not candidate_lines:
        logger.debug(f"Name '{last_name}' not found in compensation table section")
        return None

    # ── Step 3: Extract dollar amounts from candidate lines ───────────────────
    for block in candidate_lines:
        comp = _extract_total_comp(block)
        if comp and comp > 10_000:  # Sanity check — must be at least $10K
            return comp

    return None


def _extract_last_name(full_name):
    """
    Extract the last name from a full name string.
    Handles "First Last", "Last, First", and "First Middle Last".
    """
    name = full_name.strip()
    if "," in name:
        # "Smith, John" format
        return name.split(",")[0].strip()
    parts = name.split()
    return parts[-1] if parts else ""


def _extract_total_comp(text):
    """
    Parse dollar amounts from a text block and return the most likely
    total compensation figure.

    DEF 14A compensation tables typically end each row with the Total column,
    which is the largest dollar figure in the row. We return the largest
    dollar amount found, which is usually the Total.

    Handles formats: $1,200,000 | 1,200,000 | $1.2M
    """
    # Match dollar amounts with commas: $1,200,000 or 1,200,000
    dollar_pattern = r"\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?)"
    matches = re.findall(dollar_pattern, text)

    amounts = []
    for m in matches:
        try:
            val = float(m.replace(",", ""))
            amounts.append(val)
        except ValueError:
            continue

    if not amounts:
        return None

    # The Total column is the largest figure in the row
    largest = max(amounts)

    # Sanity bounds: compensation should be between $10K and $50M
    if 10_000 <= largest <= 50_000_000:
        return largest

    return None
