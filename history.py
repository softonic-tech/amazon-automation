"""
Persistent product history for cross-run deduplication.

Every product the pipeline finishes analyzing is recorded here so future
runs can skip it — protecting Keepa tokens (~6-8 tokens each) that would
otherwise be spent re-analyzing products already in the client's Excel
export.

Dedup key: supplier URL (deterministic, no false positives). UPC and
ASIN are indexed too so future queries like "have we ever seen this
UPC?" are cheap, but the primary lookup path is URL.

Storage:
    SQLite file at $HISTORY_DB_PATH (default: ./data/history.db).
    Chosen over JSON so lookups on 100k+ rows stay O(log n) and
    concurrent readers from the Flask worker + scraper don't collide.

Usage:
    from history import RunHistory
    hist = RunHistory()  # opens/creates data/history.db
    new_urls = hist.filter_new(discovered_urls)  # drops already-seen
    hist.record(url="...", upc="...", asin="...", run_id="abc123")
    hist.reset()  # wipes the table
    stats = hist.stats()  # {"total": 4231, "oldest": "2026-08-01", ...}
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("scraper.history")

# --- Location resolution ---
# Prefer explicit env var (set by deploy script). Fall back to a `data/`
# folder next to this module so local dev works without configuration.
_DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "history.db"


def _resolve_path() -> Path:
    override = os.environ.get("HISTORY_DB_PATH")
    if override:
        return Path(override).expanduser()
    return _DEFAULT_DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_history (
    supplier_url TEXT PRIMARY KEY,
    upc          TEXT,
    asin         TEXT,
    first_seen   INTEGER NOT NULL,
    run_id       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ph_upc  ON product_history(upc);
CREATE INDEX IF NOT EXISTS idx_ph_asin ON product_history(asin);
CREATE INDEX IF NOT EXISTS idx_ph_seen ON product_history(first_seen);
"""


class RunHistory:
    """
    Thread-safe wrapper around the product-history SQLite DB.

    All connections are created per-call (SQLite connections are cheap
    and this avoids the 'connection created in different thread' error
    when the Flask job scheduler hands work between threads).
    """

    def __init__(self, db_path: str | Path | None = None):
        self._path = Path(db_path) if db_path else _resolve_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize writes across threads. SQLite handles multi-reader
        # fine but writer contention causes "database is locked" errors
        # under heavy Flask + scraper concurrency.
        self._write_lock = threading.Lock()
        self._ensure_schema()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        # `check_same_thread=False` is safe because we serialize writes
        # via _write_lock and reads can genuinely happen from anywhere.
        conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            timeout=10.0,
        )
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- Lookup ----------

    def already_seen(self, url: str) -> bool:
        if not url:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM product_history WHERE supplier_url = ? LIMIT 1",
                (url,),
            )
            return cur.fetchone() is not None

    def filter_new(self, urls: list[str]) -> tuple[list[str], list[str]]:
        """
        Split `urls` into (new, already_seen). Order of `new` matches
        the input order so downstream sampling stays deterministic.
        """
        if not urls:
            return [], []
        with self._connect() as conn:
            # Batch the lookup — one query per URL would be slow at 4k+ URLs.
            # SQLite's default parameter limit is 999, so chunk the IN clause.
            seen: set[str] = set()
            for start in range(0, len(urls), 900):
                chunk = urls[start:start + 900]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"SELECT supplier_url FROM product_history "
                    f"WHERE supplier_url IN ({placeholders})",
                    chunk,
                )
                seen.update(row["supplier_url"] for row in cur.fetchall())
        new = [u for u in urls if u not in seen]
        skipped = [u for u in urls if u in seen]
        return new, skipped

    # ---------- Recording ----------

    def record(
        self,
        url: str,
        upc: str | None = None,
        asin: str | None = None,
        run_id: str = "",
    ) -> None:
        if not url:
            return
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO product_history "
                "(supplier_url, upc, asin, first_seen, run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (url, upc or None, asin or None, int(time.time()), run_id),
            )

    def record_many(self, rows: list[dict]) -> int:
        """
        Bulk-insert a list of {url, upc, asin, run_id} dicts. Returns
        the number of NEW rows actually written (existing URLs are
        left untouched because of the PRIMARY KEY on supplier_url).
        """
        if not rows:
            return 0
        now = int(time.time())
        values = [
            (r["url"], r.get("upc"), r.get("asin"), now, r.get("run_id", ""))
            for r in rows
            if r.get("url")
        ]
        if not values:
            return 0
        with self._write_lock, self._connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM product_history"
            ).fetchone()["n"]
            conn.executemany(
                "INSERT OR IGNORE INTO product_history "
                "(supplier_url, upc, asin, first_seen, run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                values,
            )
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM product_history"
            ).fetchone()["n"]
        return after - before

    # ---------- Management ----------

    def reset(self) -> int:
        """Wipe every entry. Returns the number of rows removed."""
        with self._write_lock, self._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM product_history"
            ).fetchone()["n"]
            conn.execute("DELETE FROM product_history")
        log.info("History reset — removed %d entries", n)
        return n

    def stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM product_history"
            ).fetchone()["n"]
            oldest_row = conn.execute(
                "SELECT MIN(first_seen) AS ts FROM product_history"
            ).fetchone()
            newest_row = conn.execute(
                "SELECT MAX(first_seen) AS ts FROM product_history"
            ).fetchone()
            distinct_runs = conn.execute(
                "SELECT COUNT(DISTINCT run_id) AS n FROM product_history"
            ).fetchone()["n"]
        return {
            "total": int(total),
            "oldest_ts": int(oldest_row["ts"]) if oldest_row["ts"] else None,
            "newest_ts": int(newest_row["ts"]) if newest_row["ts"] else None,
            "distinct_runs": int(distinct_runs),
            "db_path": str(self._path),
        }
