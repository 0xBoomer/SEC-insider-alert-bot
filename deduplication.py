"""
deduplication.py — Track processed filing accession numbers in processed.json.

Prevents the same Form 4 filing from triggering duplicate alerts across runs.
The file is a simple JSON array of accession number strings:
    ["0001234567-24-000001", "0001234567-24-000002", ...]

On GitHub Actions, the file is committed back to the repo after each run
(see run.yml) so it persists across workflow invocations.

We cap the list at MAX_ENTRIES to prevent unbounded file growth.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

PROCESSED_FILE = os.path.join(os.path.dirname(__file__), "processed.json")

# Keep at most this many accession numbers in the file.
# At ~40 new Form 4s/day filtered to open-market buys, 3,000 ≈ ~75 trading days.
MAX_ENTRIES = 3_000


def load_processed():
    """Return the set of already-processed accession numbers."""
    if not os.path.exists(PROCESSED_FILE):
        return set()

    try:
        with open(PROCESSED_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        logger.warning("processed.json has unexpected format — starting fresh")
        return set()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read processed.json: {e}")
        return set()


def save_processed(processed_set):
    """
    Persist the updated set back to processed.json.
    Trims to the most recent MAX_ENTRIES entries (order-independent, but
    we convert to list for JSON serialisation).
    """
    entries = list(processed_set)

    # If we've grown too large, drop the oldest entries.
    # Since sets are unordered we can't trim precisely, but this is good enough.
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]

    try:
        with open(PROCESSED_FILE, "w") as f:
            json.dump(entries, f, indent=2)
        logger.debug(f"Saved {len(entries)} accession numbers to processed.json")
    except OSError as e:
        logger.error(f"Could not write processed.json: {e}")


def is_processed(accession_no, processed_set):
    """Return True if this accession number has already been handled."""
    return accession_no in processed_set


def mark_processed(accession_no, processed_set):
    """Add an accession number to the in-memory set (call save_processed after)."""
    processed_set.add(accession_no)
