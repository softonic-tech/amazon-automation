"""
SQLite persistence for jobs and daily schedules.

The Flask process already runs on the VPS (systemd), so a search keeps
going after the client's PC shuts down. This module makes that durable:

  * schedules survive gunicorn restarts
  * finished jobs keep their Excel path so yesterday's run is downloadable
    when the client logs back in

File: $JOBS_DB_PATH or ./data/jobs.db (next to history.db).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("webapp.persist")

_DEFAULT = Path(__file__).resolve().parent / "data" / "jobs.db"
SCHEDULE_TZ = ZoneInfo(os.environ.get("SCHEDULE_TZ", "America/Chicago"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    mode         TEXT,
    payload_json TEXT,
    summary_json TEXT,
    output_file  TEXT,
    error        TEXT,
    source       TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

CREATE TABLE IF NOT EXISTS schedules (
    id           TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL DEFAULT 1,
    hour         INTEGER NOT NULL,
    minute       INTEGER NOT NULL,
    tz           TEXT NOT NULL DEFAULT 'America/Chicago',
    payload_json TEXT NOT NULL,
    label        TEXT,
    next_run_at  REAL,
    last_run_at  REAL,
    last_job_id  TEXT,
    created_at   REAL NOT NULL
);
"""


def _db_path() -> Path:
    override = os.environ.get("JOBS_DB_PATH")
    if override:
        return Path(override).expanduser()
    return _DEFAULT


class JobStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = Path(db_path) if db_path else _db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- jobs ----------

    def upsert_job(self, job: dict, payload: dict | None = None) -> None:
        now = time.time()
        payload_json = json.dumps(payload) if payload is not None else None
        summary = job.get("summary")
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM jobs WHERE id = ?", (job["id"],)
            ).fetchone()
            if existing and payload_json is None:
                payload_json = existing["payload_json"]
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, mode, payload_json, summary_json, output_file,
                    error, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    summary_json = excluded.summary_json,
                    output_file = excluded.output_file,
                    error = excluded.error,
                    source = excluded.source,
                    updated_at = excluded.updated_at,
                    payload_json = COALESCE(excluded.payload_json, jobs.payload_json)
                """,
                (
                    job["id"],
                    job.get("status") or "queued",
                    job.get("mode") or "scrape",
                    payload_json,
                    json.dumps(summary) if summary is not None else None,
                    job.get("output_file"),
                    job.get("error"),
                    job.get("requested_source"),
                    job.get("created_at") or now,
                    now,
                ),
            )

    def recent_jobs(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            if item.get("summary_json"):
                try:
                    item["summary"] = json.loads(item["summary_json"])
                except json.JSONDecodeError:
                    item["summary"] = None
            else:
                item["summary"] = None
            item.pop("summary_json", None)
            item.pop("payload_json", None)
            out.append(item)
        return out

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("summary_json"):
            try:
                item["summary"] = json.loads(item["summary_json"])
            except json.JSONDecodeError:
                item["summary"] = None
        return item

    def load_into(self, jobs: dict, lock: threading.Lock) -> int:
        """Hydrate the in-memory JOBS dict from the last few persisted rows."""
        n = 0
        for row in self.recent_jobs(40):
            jid = row["id"]
            with lock:
                if jid in jobs:
                    continue
                jobs[jid] = {
                    "id": jid,
                    "mode": row.get("mode") or "scrape",
                    "status": row.get("status") or "done",
                    "stage": row.get("status") or "done",
                    "log": [],
                    "error": row.get("error"),
                    "output_file": row.get("output_file"),
                    "summary": row.get("summary"),
                    "created_at": row.get("created_at") or 0,
                    "requested_source": row.get("source"),
                    "keepa_exhausted": False,
                    "keepa_tokens_left": None,
                    "current": None,
                    "total": None,
                    "current_activity": None,
                    "dedup_skipped": None,
                    "dedup_all_seen": False,
                }
                n += 1
        return n

    # ---------- schedules ----------

    def add_schedule(
        self,
        payload: dict,
        hour: int,
        minute: int,
        label: str | None = None,
        tz_name: str = "America/Chicago",
    ) -> dict:
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        next_at = _next_run_ts(hour, minute, tz_name)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedules (
                    id, enabled, hour, minute, tz, payload_json, label,
                    next_run_at, created_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid, int(hour), int(minute), tz_name,
                    json.dumps(payload), label or _default_label(payload, hour, minute),
                    next_at, now,
                ),
            )
        return self.get_schedule(sid) or {}

    def get_schedule(self, sid: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (sid,)
            ).fetchone()
        return _sched_row(row) if row else None

    def list_schedules(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules ORDER BY hour, minute, created_at"
            ).fetchall()
        return [_sched_row(r) for r in rows]

    def set_enabled(self, sid: str, enabled: bool) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE schedules SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, sid),
            )

    def delete_schedule(self, sid: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM schedules WHERE id = ?", (sid,))
            return cur.rowcount > 0

    def claim_due(self) -> list[dict]:
        """
        Return due enabled schedules and push next_run_at forward so two
        overlapping ticks cannot double-fire the same row.
        """
        now = time.time()
        claimed: list[dict] = []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL "
                "AND next_run_at <= ?",
                (now,),
            ).fetchall()
            for row in rows:
                nxt = _next_run_ts(row["hour"], row["minute"], row["tz"] or "America/Chicago")
                # If next_run landed in the past (clock jump), skip ahead a day.
                if nxt <= now:
                    nxt = _next_run_ts(
                        row["hour"], row["minute"], row["tz"] or "America/Chicago",
                        from_ts=now + 60,
                    )
                conn.execute(
                    "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                    (now, nxt, row["id"]),
                )
                claimed.append(_sched_row(row))
        return claimed

    def mark_launched(self, sid: str, job_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE schedules SET last_job_id = ? WHERE id = ?",
                (job_id, sid),
            )


def _sched_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["payload"] = json.loads(item.get("payload_json") or "{}")
    except json.JSONDecodeError:
        item["payload"] = {}
    item.pop("payload_json", None)
    item["enabled"] = bool(item.get("enabled"))
    return item


def _default_label(payload: dict, hour: int, minute: int) -> str:
    url = (payload.get("sitemap_url") or "").replace("https://", "").replace("www.", "")
    host = url.split("/")[0] or "supplier"
    n = payload.get("limit") or "?"
    brands = (payload.get("brands") or "").strip()
    bit = f"{host} · {n} products"
    if brands:
        bit += f" · {brands[:40]}"
    return f"{bit} · daily {hour:02d}:{minute:02d}"


def _next_run_ts(
    hour: int,
    minute: int,
    tz_name: str,
    from_ts: float | None = None,
) -> float:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = SCHEDULE_TZ
    now = datetime.fromtimestamp(from_ts, tz) if from_ts else datetime.now(tz)
    candidate = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()
