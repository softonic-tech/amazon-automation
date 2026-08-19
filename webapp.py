"""
Sourcing Pipeline — Local Web UI (Automation-first redesign)
============================================================
Non-technical UI for the scraping + Keepa + sourcing pipeline.

Client workflow:
  1. Pick supplier (Zoro or iHerb)
  2. Pick how many products to analyze
  3. Pick approval criteria (preset)
  4. Click Start
  5. Download the resulting xlsx

Advanced filter controls are tucked into a "Adjust individual filters"
disclosure — hidden by default so the client doesn't see them unless
they want to.

Usage:
    pip install flask openpyxl
    python webapp.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask, abort, jsonify, redirect, render_template_string,
    request, send_file, session, url_for,
)

from sourcing import SourcingConfig

# ---------------------------------------------------------------- #
# Setup                                                            #
# ---------------------------------------------------------------- #

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("webapp")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / ".webapp_runs"
WORK_DIR.mkdir(exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------- #
# Auth: signed-cookie sessions (no database, ~microseconds/req)    #
# ---------------------------------------------------------------- #

# SECRET_KEY signs the session cookie. Must be stable across restarts, otherwise
# every user gets logged out on redeploy. setup_vps.sh generates one and writes
# it to .env. If missing (dev), fall back to an ephemeral one and warn.
_secret = os.environ.get("SECRET_KEY", "").strip()
if not _secret:
    log.warning("SECRET_KEY not set — using ephemeral key. Sessions won't "
                "survive restart. Set SECRET_KEY in .env for production.")
    _secret = secrets.token_hex(32)
app.config["SECRET_KEY"] = _secret
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Set SESSION_COOKIE_SECURE=1 in .env once you're behind HTTPS.
if os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
    app.config["SESSION_COOKIE_SECURE"] = True

APP_USERNAME = os.environ.get("APP_USERNAME", "").strip()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
AUTH_ENABLED = bool(APP_USERNAME and APP_PASSWORD)
if not AUTH_ENABLED:
    log.warning("APP_USERNAME/APP_PASSWORD not set — auth is DISABLED (dev "
                "mode). Set both in .env for production.")


def require_login(fn):
    """Redirect HTML routes to /login, return 401 JSON for /api/*."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not AUTH_ENABLED or session.get("user") == APP_USERNAME:
            return fn(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login", next=request.path))
    return wrapper


# ---------------------------------------------------------------- #
# Presets — three clear approval-strictness levels                 #
# ---------------------------------------------------------------- #

PRESETS = {
    "balanced": {
        "label": "Balanced",
        "sub": "Your standard rules",
        "desc": "At least 4 FBM sellers, 3.5+ rating, positive profit with 10% margin.",
        "values": {
            "min_profit": 2, "min_margin": 0.10, "min_roi": 0.15,
            "min_fbm_sellers": 4, "min_rating": 3.5, "min_reviews": 0,
            "require_rating": False, "require_reviews": False,
            "reject_if_amazon_on_listing": False, "max_bsr": 0,
            "min_historical_sellers": 0,
        },
    },
    "loose": {
        "label": "Discovery",
        "sub": "See more, filter less",
        "desc": "Almost no filtering. Good for exploring what's out there before narrowing.",
        "values": {
            "min_profit": 0.01, "min_margin": 0, "min_roi": 0,
            "min_fbm_sellers": 0, "min_rating": 0, "min_reviews": 0,
            "require_rating": False, "require_reviews": False,
            "reject_if_amazon_on_listing": False, "max_bsr": 0,
            "min_historical_sellers": 0,
        },
    },
    "strict": {
        "label": "Amazon-safe",
        "sub": "Only clear winners",
        "desc": "Requires 50+ reviews, 4.0+ rating, healthy BSR, and 15% margin. Fewer, more confident.",
        "values": {
            "min_profit": 5, "min_margin": 0.15, "min_roi": 0.25,
            "min_fbm_sellers": 4, "min_rating": 4.0, "min_reviews": 50,
            "require_rating": True, "require_reviews": True,
            "reject_if_amazon_on_listing": False, "max_bsr": 100000,
            "min_historical_sellers": 5,
        },
    },
}

RETAILERS = [
    {
        "value": "https://www.zoro.com",
        "label": "Zoro",
        "sub": "US supplier | prices in USD",
    },
    {
        "value": "https://www.iherb.com",
        "label": "iHerb",
        "sub": "supplements | US pricing forced",
    },
]


# ---------------------------------------------------------------- #
# Job execution                                                    #
# ---------------------------------------------------------------- #

def _make_job(mode: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "mode": mode,
            "status": "queued",
            "stage": "starting",
            "log": [],
            "error": None,
            "output_file": None,
            "summary": None,
            "created_at": time.time(),
            "keepa_exhausted": False,   # sticky flag; set from log scan
            "keepa_tokens_left": None,  # last known token balance
            # Live progress fields (populated by _append_log regex scan)
            "current": None,             # e.g. 15
            "total": None,               # e.g. 25
            "current_activity": None,    # human-readable line, e.g. "Keepa UPC 12345"
            "urls_discovered": None,     # from "Sitemap yielded N URL(s)"
            "urls_after_filter": None,   # from "N remain for scraping"
            "eta_seconds": None,         # simple linear extrapolation
            # ETA baseline — snapshotted on first N/M seen in current stage
            "_eta_stage": None,
            "_eta_start_time": None,
            "_eta_start_count": None,
        }
    return job_id


# --- Regex anchors for log-line progress extraction ---
# These patterns match lines emitted by scraper.py and sourcing.py. Keep in
# sync with those modules — if you rename a log message there, update here.
# NOTE: scraper.py's logger prepends "HH:MM:SS [INFO] " to every line, so
# _RE_STEP uses `search` (not `match`) with a whitespace anchor before `[N/M]`
# to avoid false positives on nested "[INFO]"-style prefixes.
_RE_STEP = re.compile(r"(?:^|\s)\[(\d+)/(\d+)\]\s+(.+?)(?:\s*$)")
_RE_URLS_YIELDED = re.compile(r"Sitemap yielded (\d+) URL", re.IGNORECASE)
_RE_URLS_KEPT = re.compile(r"(\d+) remain(?:ing)?(?:\s+for scraping)?",
                           re.IGNORECASE)
_RE_TOKENS = re.compile(r"Keepa tokens remaining:\s*(\d+)", re.IGNORECASE)
_RE_FETCHING = re.compile(r"Fetching\s+(https?://\S+)")


def _summarize_step(rest: str) -> str:
    """
    Turn a raw '[N/M] ...' line body into a compact human-readable
    'current activity' string for the progress card. Kept short — the UI
    shows this inline next to the progress bar.
    """
    if "Keepa UPC" in rest:
        upc = rest.split("Keepa UPC", 1)[1].strip()[:20]
        return f"Amazon lookup for UPC {upc}"
    if "Keepa title search" in rest:
        after = rest.split("Keepa title search", 1)[1].strip()
        return f"Amazon title search: {after[:50]}"
    if "Scraping product" in rest:
        return "Scraping supplier product page"
    return rest[:80]


def _update_eta(job: dict, current: int, total: int) -> None:
    """
    Simple linear-rate ETA. Snapshots progress on entry to each stage so
    a slow first item doesn't skew the estimate for the whole run.
    """
    if total <= 0 or current <= 0:
        return
    stage = job.get("stage")
    now = time.time()
    # Reset baseline whenever stage changes so ETA is per-stage.
    if job.get("_eta_stage") != stage:
        job["_eta_stage"] = stage
        job["_eta_start_time"] = now
        job["_eta_start_count"] = current - 1  # count as if we just started
    elapsed = now - (job["_eta_start_time"] or now)
    done_since_baseline = current - (job["_eta_start_count"] or 0)
    if elapsed >= 3 and done_since_baseline > 0:
        rate = done_since_baseline / elapsed  # items/sec
        remaining = max(0, total - current)
        if rate > 0:
            job["eta_seconds"] = int(remaining / rate)


def _append_log(job_id: str, line: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        job["log"].append(line)
        if len(job["log"]) > 500:
            job["log"] = job["log"][-500:]

        low = line.lower()
        if "sitemap" in low or "discovered" in low or "collected" in low:
            job["stage"] = "discovering"
        elif "scraping product" in low or "fetching " in low:
            job["stage"] = "scraping"
        elif "keepa" in low and ("lookup" in low or "upc" in low or "title search" in low):
            job["stage"] = "matching"
        elif "sourcing done" in low or "wrote " in low and "sourcing rows" in low:
            job["stage"] = "finalizing"

        # Surface Keepa quota-exhaustion state to the UI so users see a
        # dedicated warning banner instead of just a confusing "some rows
        # rejected" message.
        if "keepa quota exhausted" in low or "keepa tokens exhausted" in low:
            job["keepa_exhausted"] = True

        # --- Structured progress extraction (per-item counters, ETA) ---

        # [N/M] step progress — anchor for scraping AND matching stages.
        # `search` is intentional: log lines are prefixed with a timestamp +
        # "[INFO]" by the scraper subprocess before hitting stdout.
        m = _RE_STEP.search(line)
        if m:
            try:
                current, total = int(m.group(1)), int(m.group(2))
                job["current"] = current
                job["total"] = total
                job["current_activity"] = _summarize_step(m.group(3))
                _update_eta(job, current, total)
            except (ValueError, IndexError):
                pass

        # Discovery counters — tell the user how many URLs were found
        m = _RE_URLS_YIELDED.search(line)
        if m:
            try:
                job["urls_discovered"] = int(m.group(1))
                job["current_activity"] = f"Discovered {m.group(1)} product URLs"
            except (ValueError, IndexError):
                pass
        m = _RE_URLS_KEPT.search(line)
        if m and "URL pre-filter" in line:
            try:
                job["urls_after_filter"] = int(m.group(1))
            except (ValueError, IndexError):
                pass

        # Live token balance (heartbeat lines from sourcing.py + final line)
        m = _RE_TOKENS.search(line)
        if m:
            try:
                job["keepa_tokens_left"] = int(m.group(1))
            except (ValueError, IndexError):
                pass

        # Also useful: individual Fetching lines when [N/M] wasn't present
        # (legacy scraper.scrape_one direct callers)
        if job.get("current_activity") is None:
            fm = _RE_FETCHING.search(line)
            if fm:
                url = fm.group(1)
                slug = url.rstrip("/").split("/")[-2 if url.endswith("/") else -1]
                job["current_activity"] = f"Fetching {slug[:60]}"


def _mark_status(job_id: str, status: str, **extra) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job["status"] = status
            for k, v in extra.items():
                job[k] = v


def _run_scraper_subprocess(job_id: str, args: list[str]) -> None:
    log.info("Job %s: %s", job_id, " ".join(args))
    _mark_status(job_id, "running")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "scraper.py", *args],
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            _append_log(job_id, line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            # Grab the last few log lines so the UI can show a useful error
            # instead of just "check the log"
            with JOBS_LOCK:
                tail = "\n".join(JOBS[job_id]["log"][-15:]) if JOBS.get(job_id) else ""
            if proc.returncode == 2:
                err = (
                    "The scraper rejected an argument (exit code 2). "
                    "This usually means scraper.py is an older version "
                    "missing a required flag. Verify with: "
                    "python scraper.py --help | Select-String \"config\"\n\n"
                    "Last log lines:\n" + tail
                )
            else:
                err = (f"Pipeline exited with code {proc.returncode}.\n\n"
                       f"Last log lines:\n{tail}")
            _mark_status(job_id, "error", error=err)
            return
    except FileNotFoundError as e:
        _mark_status(job_id, "error", error=f"scraper.py not found: {e}")
        return
    except Exception as e:  # noqa: BLE001
        _mark_status(job_id, "error", error=str(e))
        return

    _mark_status(job_id, "summarizing")


def _summarize_xlsx(job_id: str, xlsx_path: Path) -> None:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(xlsx_path, data_only=True)
        ws = wb.active
        headers = [c.value for c in ws[2]]

        def col(name: str) -> int | None:
            try:
                return headers.index(name)
            except ValueError:
                return None

        c_status = col("Status")
        c_reasons = col("Reject Reasons")
        c_title = col("Supplier Title")
        c_profit = col("Profit $")
        c_roi = col("ROI %")
        c_asin = col("Amazon ASIN")
        c_supplier_cost = col("Supplier Cost (USD)")
        c_amazon_price = col("Amazon Sell Price")
        c_fbm = col("FBM Sellers (live)")

        by_status: dict[str, int] = {}
        approved_rows: list[dict] = []
        reason_tally: dict[str, int] = {}

        for row in ws.iter_rows(min_row=3, values_only=True):
            if c_status is None:
                continue
            status = row[c_status] or "Unknown"
            by_status[status] = by_status.get(status, 0) + 1

            if status == "APPROVED":
                approved_rows.append({
                    "title": (row[c_title] or "") if c_title is not None else "",
                    "profit": row[c_profit] if c_profit is not None else None,
                    "roi": row[c_roi] if c_roi is not None else None,
                    "asin": row[c_asin] if c_asin is not None else None,
                    "supplier_cost": row[c_supplier_cost] if c_supplier_cost is not None else None,
                    "amazon_price": row[c_amazon_price] if c_amazon_price is not None else None,
                    "fbm": row[c_fbm] if c_fbm is not None else None,
                })

            if c_reasons is not None and status == "Rejected":
                for reason in (row[c_reasons] or "").split(";"):
                    reason = reason.strip()
                    if not reason:
                        continue
                    if "FBM sellers" in reason:
                        key = "Not enough FBM sellers"
                    elif "profit" in reason.lower():
                        key = "Profit too low"
                    elif "Amazon is on" in reason:
                        key = "Amazon competes on the listing"
                    elif "rating" in reason.lower():
                        key = "Rating too low"
                    elif "reviews" in reason.lower():
                        key = "Not enough reviews"
                    elif "BSR" in reason:
                        key = "Selling too slowly (BSR)"
                    elif "no Amazon match" in reason:
                        key = "Not on Amazon"
                    elif "out of stock" in reason.lower():
                        key = "Out of stock at supplier"
                    elif "margin" in reason.lower():
                        key = "Margin too thin"
                    elif "ROI" in reason:
                        key = "ROI too low"
                    else:
                        key = reason[:60]
                    reason_tally[key] = reason_tally.get(key, 0) + 1

        approved_rows.sort(
            key=lambda r: r["profit"] if isinstance(r["profit"], (int, float)) else -999,
            reverse=True,
        )

        summary = {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "top_approved": approved_rows[:10],
            "reject_reasons": sorted(reason_tally.items(), key=lambda kv: -kv[1])[:8],
        }
        _mark_status(job_id, "done",
                     output_file=str(xlsx_path.resolve()),
                     summary=summary)
    except Exception as e:  # noqa: BLE001
        log.exception("Summary error for job %s", job_id)
        _mark_status(job_id, "error", error=f"Summary failed: {e}")


def _job_scrape_and_analyze(job_id: str, params: dict) -> None:
    try:
        cfg_path = WORK_DIR / f"{job_id}_config.json"
        cfg_path.write_text(json.dumps(params["config"], indent=2), encoding="utf-8")

        out_path = WORK_DIR / f"{job_id}_result.xlsx"

        args = [
            "--sitemap", params["sitemap_url"],
            "--limit", str(params["limit"]),
            "--sourcing",
            "--config", str(cfg_path),
            "--out", str(out_path),
            "--no-robots",
        ]
        if params.get("random"):
            args.append("--random")
        if params.get("min_supplier_price", 0) > 0:
            args.extend(["--min-supplier-price", str(params["min_supplier_price"])])
        if params.get("brands"):
            args.extend(["--brands", params["brands"]])

        _run_scraper_subprocess(job_id, args)
        if JOBS[job_id]["status"] == "error":
            return
        _summarize_xlsx(job_id, out_path)
    except Exception as e:  # noqa: BLE001
        log.exception("Job %s failed", job_id)
        _mark_status(job_id, "error", error=str(e))


# ---------------------------------------------------------------- #
# Routes                                                           #
# ---------------------------------------------------------------- #

@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    if session.get("user") == APP_USERNAME:
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        remember = request.form.get("remember") == "on"
        # secrets.compare_digest is constant-time — resists timing attacks.
        if (secrets.compare_digest(u, APP_USERNAME)
                and secrets.compare_digest(p, APP_PASSWORD)):
            session.clear()
            session["user"] = APP_USERNAME
            session.permanent = bool(remember)
            next_url = request.args.get("next", "/")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = "/"
            return redirect(next_url)
        # Small delay makes brute-force annoying without hurting real UX.
        time.sleep(0.6)
        error = "That username or password isn't right. Try again."

    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_login
def index():
    return render_template_string(
        INDEX_HTML,
        presets=PRESETS,
        retailers=RETAILERS,
        username=session.get("user") or "",
        auth_enabled=AUTH_ENABLED,
    )


@app.route("/api/run", methods=["POST"])
@require_login
def api_run():
    data = request.get_json(force=True)
    required = ["sitemap_url", "limit", "config"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"Missing: {missing}"}), 400
    job_id = _make_job("scrape")
    threading.Thread(
        target=_job_scrape_and_analyze,
        args=(job_id, data), daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
@require_login
def api_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "stage": job["stage"],
            "log_tail": job["log"][-25:],
            "log_count": len(job["log"]),
            "error": job["error"],
            "summary": job["summary"],
            "keepa_exhausted": job.get("keepa_exhausted", False),
            "keepa_tokens_left": job.get("keepa_tokens_left"),
            "current": job.get("current"),
            "total": job.get("total"),
            "current_activity": job.get("current_activity"),
            "urls_discovered": job.get("urls_discovered"),
            "urls_after_filter": job.get("urls_after_filter"),
            "eta_seconds": job.get("eta_seconds"),
        })


@app.route("/api/download/<job_id>")
@require_login
def api_download(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or not job.get("output_file"):
        abort(404)
    return send_file(
        job["output_file"],
        as_attachment=True,
        download_name=f"sourcing_{job_id}.xlsx",
    )


# ---------------------------------------------------------------- #
# HTML template                                                    #
# ---------------------------------------------------------------- #

LOGIN_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in — Sourcing</title>
  <style>
    :root {
      --paper:        #F7F4EC;
      --paper-warm:   #EDE7D6;
      --card:         #FFFFFF;
      --border:       #E4DECE;
      --border-strong:#C9C0AB;
      --ink:          #1C1B1A;
      --graphite:     #4A4744;
      --muted:        #857F76;
      --brand:        #1C3E36;
      --brand-hover:  #142B26;
      --brand-soft:   #E8EEE9;
      --rejected:     #9A3F2C;
      --rejected-soft:#F0DDD5;
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; height: 100%; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                   Roboto, "Helvetica Neue", Arial, sans-serif;
      background: var(--paper);
      color: var(--ink);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 24px;
    }

    .stage {
      width: 100%;
      max-width: 420px;
    }

    header.brand {
      display: flex;
      align-items: center;
      gap: 12px;
      justify-content: center;
      margin-bottom: 32px;
    }
    .brand-mark {
      width: 40px; height: 40px;
      display: flex; align-items: center; justify-content: center;
      background: var(--brand);
      color: var(--paper);
      border-radius: 3px;
      font-family: "Iowan Old Style", "Palatino Linotype", Georgia, serif;
      font-size: 22px;
      line-height: 1;
    }
    .brand-text .name {
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 20px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }
    .brand-text .sub {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }

    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 40px 36px 32px;
      box-shadow: 0 1px 0 rgba(28, 27, 26, 0.03),
                  0 8px 24px rgba(28, 27, 26, 0.04);
    }

    h1.title {
      font-family: "Iowan Old Style", "Palatino Linotype", Georgia, serif;
      font-size: 28px;
      font-weight: 400;
      line-height: 1.2;
      letter-spacing: -0.02em;
      margin: 0 0 6px;
    }
    h1.title em {
      font-style: italic;
      color: var(--brand);
    }
    .subtitle {
      color: var(--graphite);
      font-size: 14px;
      margin: 0 0 28px;
    }

    form { display: flex; flex-direction: column; gap: 18px; }

    .field { display: flex; flex-direction: column; gap: 6px; }
    .field > label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.1em;
      font-weight: 600;
    }
    .field input {
      appearance: none;
      -webkit-appearance: none;
      background: var(--paper);
      border: 1px solid var(--border-strong);
      border-radius: 3px;
      padding: 12px 14px;
      font: inherit;
      font-size: 15px;
      color: var(--ink);
      transition: border-color 0.12s ease, background 0.12s ease;
    }
    .field input:focus {
      outline: none;
      border-color: var(--brand);
      background: #fff;
    }

    .remember {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--graphite);
      cursor: pointer;
      user-select: none;
    }
    .remember input {
      width: 16px; height: 16px;
      accent-color: var(--brand);
      cursor: pointer;
    }

    button.submit {
      background: var(--brand);
      color: var(--paper);
      border: none;
      border-radius: 3px;
      padding: 13px 20px;
      font: inherit;
      font-size: 15px;
      font-weight: 600;
      letter-spacing: 0.01em;
      cursor: pointer;
      transition: background 0.12s ease;
      margin-top: 4px;
    }
    button.submit:hover { background: var(--brand-hover); }
    button.submit:active { transform: translateY(1px); }
    button.submit:focus-visible {
      outline: 2px solid var(--brand);
      outline-offset: 2px;
    }

    .error {
      background: var(--rejected-soft);
      border: 1px solid rgba(154, 63, 44, 0.25);
      color: var(--rejected);
      border-radius: 3px;
      padding: 10px 14px;
      font-size: 13px;
      margin: 0 0 4px;
    }

    .footnote {
      text-align: center;
      color: var(--muted);
      font-size: 12px;
      margin-top: 20px;
    }
  </style>
</head>
<body>
  <div class="stage">
    <header class="brand">
      <div class="brand-mark">S</div>
      <div class="brand-text">
        <div class="name">Sourcing</div>
        <div class="sub">Amazon FBM discovery</div>
      </div>
    </header>

    <div class="card">
      <h1 class="title">Welcome <em>back</em>.</h1>
      <p class="subtitle">Sign in to continue.</p>

      {% if error %}<div class="error">{{ error }}</div>{% endif %}

      <form method="post" autocomplete="on">
        <div class="field">
          <label for="username">Username</label>
          <input id="username" name="username" type="text"
                 autocomplete="username" required autofocus>
        </div>
        <div class="field">
          <label for="password">Password</label>
          <input id="password" name="password" type="password"
                 autocomplete="current-password" required>
        </div>
        <label class="remember">
          <input type="checkbox" name="remember" checked>
          Keep me signed in for 30 days
        </label>
        <button type="submit" class="submit">Sign in</button>
      </form>
    </div>

    <p class="footnote">Contact your admin if you can't sign in.</p>
  </div>
</body>
</html>
"""


INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sourcing</title>
  <style>
    :root {
      --paper:        #F7F4EC;
      --paper-warm:   #EDE7D6;
      --card:         #FFFFFF;
      --border:       #E4DECE;
      --border-strong:#C9C0AB;
      --ink:          #1C1B1A;
      --graphite:     #4A4744;
      --muted:        #857F76;
      --brand:        #1C3E36;
      --brand-soft:   #E8EEE9;
      --approved:     #2F6B47;
      --approved-soft:#E4EEE6;
      --review:       #B87A18;
      --review-soft:  #F5E9CE;
      --rejected:     #9A3F2C;
      --rejected-soft:#F0DDD5;
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                   Roboto, "Helvetica Neue", Arial, sans-serif;
      background: var(--paper);
      color: var(--ink);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }
    .serif {
      font-family: "Iowan Old Style", "Palatino Linotype", "Palatino",
                   "Book Antiqua", Georgia, serif;
    }

    .container {
      max-width: 780px;
      margin: 0 auto;
      padding: 32px 24px 96px;
    }

    header.brand {
      display: flex;
      align-items: center;
      gap: 12px;
      padding-bottom: 40px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 48px;
    }
    .brand-mark {
      width: 34px; height: 34px;
      display: flex; align-items: center; justify-content: center;
      background: var(--brand);
      color: var(--paper);
      border-radius: 2px;
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 20px;
      line-height: 1;
    }
    .brand-text .name {
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 18px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }
    .brand-text .sub {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }
    .user-menu {
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .user-name {
      font-size: 13px;
      color: var(--graphite);
      font-weight: 500;
    }
    .signout {
      font-size: 12px;
      color: var(--muted);
      text-decoration: none;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 600;
      padding: 6px 10px;
      border: 1px solid var(--border-strong);
      border-radius: 3px;
      transition: color 0.12s ease, border-color 0.12s ease, background 0.12s ease;
    }
    .signout:hover {
      color: var(--brand);
      border-color: var(--brand);
      background: var(--brand-soft);
    }

    .hero { margin-bottom: 40px; }
    .hero h1 {
      font-family: "Iowan Old Style", "Palatino Linotype", "Palatino", Georgia, serif;
      font-size: 36px;
      font-weight: 400;
      line-height: 1.2;
      letter-spacing: -0.02em;
      margin: 0 0 12px;
      color: var(--ink);
    }
    .hero h1 em {
      font-style: italic;
      color: var(--brand);
      font-weight: 400;
    }
    .hero p {
      color: var(--graphite);
      font-size: 15px;
      margin: 0;
      max-width: 540px;
    }

    .run-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 4px;
      overflow: hidden;
    }
    .run-body { padding: 32px; }

    .fieldset {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 24px;
      margin-bottom: 32px;
    }
    @media (max-width: 560px) { .fieldset { grid-template-columns: 1fr; } }

    .field { display: flex; flex-direction: column; gap: 8px; }
    .field > label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.1em;
      font-weight: 600;
    }
    .field select, .field input[type="number"], .field input[type="text"] {
      appearance: none;
      -webkit-appearance: none;
      background: var(--paper);
      border: 1px solid var(--border-strong);
      border-radius: 2px;
      padding: 12px 14px;
      font-size: 15px;
      color: var(--ink);
      font-family: inherit;
      transition: border-color 0.15s, background 0.15s;
    }
    .field-hint {
      font-size: 11px;
      color: var(--muted);
      line-height: 1.4;
    }
    .field select {
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8' fill='none'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' stroke='%234A4744' stroke-width='1.4'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 14px center;
      padding-right: 40px;
    }
    .field input:focus, .field select:focus {
      outline: none;
      border-color: var(--brand);
      background: white;
    }

    .preset-row {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 12px;
    }
    @media (max-width: 640px) { .preset-row { grid-template-columns: 1fr; } }
    .preset {
      background: var(--paper);
      border: 1.5px solid var(--border-strong);
      border-radius: 3px;
      padding: 16px 14px;
      text-align: left;
      cursor: pointer;
      font-family: inherit;
      color: var(--ink);
      transition: all 0.15s;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .preset:hover {
      border-color: var(--brand);
      background: var(--brand-soft);
    }
    .preset.selected {
      border-color: var(--brand);
      background: var(--brand-soft);
      box-shadow: inset 3px 0 0 var(--brand);
    }
    .preset-label {
      font-weight: 600;
      font-size: 14px;
    }
    .preset-sub {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .preset-desc {
      font-size: 12px;
      color: var(--graphite);
      line-height: 1.4;
      margin-top: 6px;
    }

    details.advanced {
      border-top: 1px solid var(--border);
      margin: 32px -32px 0;
      padding: 0 32px;
    }
    details.advanced summary {
      cursor: pointer;
      padding: 20px 0;
      font-size: 13px;
      font-weight: 600;
      color: var(--brand);
      list-style: none;
      display: flex;
      align-items: center;
      gap: 10px;
      user-select: none;
    }
    details.advanced summary::-webkit-details-marker { display: none; }
    details.advanced summary::before {
      content: "+";
      display: inline-block;
      width: 18px; height: 18px;
      background: var(--brand);
      color: var(--paper);
      border-radius: 2px;
      text-align: center;
      line-height: 17px;
      font-weight: 700;
      font-size: 14px;
    }
    details.advanced[open] summary::before { content: "-"; }

    .advanced-body {
      padding-bottom: 24px;
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 20px;
    }
    @media (max-width: 640px) { .advanced-body { grid-template-columns: 1fr 1fr; } }

    .adv-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .adv-group > label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 600;
    }
    .adv-group input[type="number"] {
      background: var(--paper);
      border: 1px solid var(--border-strong);
      border-radius: 2px;
      padding: 8px 10px;
      font-size: 14px;
      color: var(--ink);
      font-family: inherit;
    }
    .adv-group input:focus {
      outline: none;
      border-color: var(--brand);
      background: white;
    }
    .adv-check {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--graphite);
      cursor: pointer;
      padding: 6px 0;
    }
    .adv-check input { margin: 0; accent-color: var(--brand); }

    .run-footer {
      background: var(--paper-warm);
      border-top: 1px solid var(--border);
      padding: 20px 32px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .run-footer .footnote {
      font-size: 12px;
      color: var(--muted);
      max-width: 320px;
      line-height: 1.4;
    }
    .btn-run {
      background: var(--brand);
      color: var(--paper);
      border: none;
      padding: 14px 32px;
      border-radius: 2px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      font-family: inherit;
      letter-spacing: 0.01em;
      transition: background 0.15s;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .btn-run::after {
      content: "->";
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 16px;
      line-height: 1;
    }
    .btn-run:hover { background: #0F2E27; }
    .btn-run:disabled {
      background: var(--muted);
      cursor: not-allowed;
    }

    .progress-card {
      display: none;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 32px;
      margin-top: 20px;
    }
    .progress-card.visible { display: block; }

    .stages {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 28px;
    }
    @media (max-width: 560px) { .stages { grid-template-columns: 1fr 1fr; } }

    .stage {
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 2px;
      background: var(--paper);
      display: flex;
      flex-direction: column;
      gap: 6px;
      opacity: 0.5;
      transition: all 0.3s;
    }
    .stage.done {
      opacity: 1;
      background: var(--approved-soft);
      border-color: var(--approved);
    }
    .stage.active {
      opacity: 1;
      background: white;
      border-color: var(--brand);
      box-shadow: 0 0 0 3px var(--brand-soft);
    }
    .stage-num {
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 22px;
      color: var(--brand);
      line-height: 1;
    }
    .stage.done .stage-num { color: var(--approved); }
    .stage.done .stage-num::after { content: " OK"; font-size: 12px; }
    .stage-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--ink);
    }
    .stage-sub {
      font-size: 11px;
      color: var(--muted);
      line-height: 1.3;
    }

    /* Live progress bar */
    .progress-bar-wrap {
      margin: 8px 0 16px;
      display: none;
    }
    .progress-bar-wrap.visible { display: block; }
    .progress-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 6px;
      font-size: 13px;
    }
    .progress-header .label {
      color: var(--ink);
      font-weight: 600;
    }
    .progress-header .counter {
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }
    .progress-bar {
      width: 100%;
      height: 8px;
      background: var(--border);
      border-radius: 4px;
      overflow: hidden;
    }
    .progress-bar-fill {
      height: 100%;
      background: var(--brand);
      transition: width 0.5s ease;
      width: 0%;
    }
    .progress-detail {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .progress-detail .activity {
      flex: 1;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .progress-detail .eta {
      color: var(--brand);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    .progress-pills {
      display: flex;
      gap: 8px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 10px;
      background: var(--paper);
      border: 1px solid var(--border);
      border-radius: 999px;
      font-size: 11px;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }
    .pill strong {
      color: var(--ink);
      font-weight: 600;
    }
    .pill.warn {
      background: #FEF3C7;
      border-color: #FDE68A;
      color: #92400E;
    }
    .pill.warn strong { color: #78350F; }

    /* Keepa-exhausted banner (shown only when tokens ran out mid-run) */
    .banner-warn {
      display: none;
      background: #FEF3C7;
      border: 1px solid #FDE68A;
      border-left: 4px solid #F59E0B;
      color: #78350F;
      padding: 12px 16px;
      border-radius: 2px;
      margin-bottom: 16px;
      font-size: 13px;
      line-height: 1.5;
    }
    .banner-warn.visible { display: block; }
    .banner-warn strong { color: #78350F; }

    .log-view {
      background: #14201C;
      color: #D8CFB8;
      font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
      font-size: 11px;
      padding: 16px;
      border-radius: 2px;
      max-height: 220px;
      overflow-y: auto;
      white-space: pre-wrap;
      line-height: 1.6;
    }

    .results-card {
      display: none;
      margin-top: 20px;
    }
    .results-card.visible { display: block; }

    .results-header {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 32px;
      margin-bottom: 20px;
    }
    .results-header h2 {
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 28px;
      font-weight: 400;
      margin: 0 0 24px;
      letter-spacing: -0.01em;
    }
    .results-header h2 em { font-style: italic; color: var(--brand); }

    .stat-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 16px;
      margin-bottom: 24px;
    }
    @media (max-width: 560px) { .stat-row { grid-template-columns: 1fr 1fr; } }

    .stat {
      padding: 16px;
      border-radius: 2px;
      border: 1px solid var(--border);
      background: var(--paper);
    }
    .stat .num {
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 40px;
      font-weight: 400;
      line-height: 1;
      letter-spacing: -0.02em;
    }
    .stat .lbl {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-top: 8px;
      font-weight: 600;
    }
    .stat.approved { background: var(--approved-soft); border-color: var(--approved); }
    .stat.approved .num { color: var(--approved); }
    .stat.review { background: var(--review-soft); border-color: var(--review); }
    .stat.review .num { color: var(--review); }
    .stat.rejected { background: var(--rejected-soft); border-color: var(--rejected); }
    .stat.rejected .num { color: var(--rejected); }
    .stat.total .num { color: var(--ink); }

    .download-cta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 20px 24px;
      background: var(--brand);
      color: var(--paper);
      border-radius: 2px;
    }
    .download-cta .txt {
      font-size: 13px;
      opacity: 0.85;
    }
    .download-cta .btn-download {
      background: var(--paper);
      color: var(--brand);
      padding: 12px 24px;
      border-radius: 2px;
      font-weight: 600;
      text-decoration: none;
      font-size: 14px;
      transition: background 0.15s;
      white-space: nowrap;
    }
    .download-cta .btn-download:hover { background: white; }

    .section-title {
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 18px;
      font-weight: 400;
      margin: 32px 0 8px;
    }
    .section-hint {
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 16px;
    }

    .approved-card, .reasons-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 8px 24px;
    }
    .approved-item {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      padding: 16px 0;
      border-bottom: 1px solid var(--border);
    }
    .approved-item:last-child { border-bottom: none; }
    .approved-item .info { min-width: 0; }
    .approved-item .title {
      font-size: 14px;
      font-weight: 500;
      color: var(--ink);
      margin-bottom: 6px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .approved-item .meta {
      font-size: 11px;
      color: var(--muted);
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
    }
    .approved-item .meta a {
      color: var(--brand);
      text-decoration: none;
    }
    .approved-item .meta a:hover { text-decoration: underline; }
    .approved-item .profit {
      text-align: right;
      font-family: "Iowan Old Style", Georgia, serif;
      font-size: 22px;
      color: var(--approved);
      line-height: 1;
      white-space: nowrap;
    }
    .approved-item .roi {
      font-size: 11px;
      color: var(--muted);
      text-align: right;
      margin-top: 6px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .reason-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 0;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
    }
    .reason-row:last-child { border-bottom: none; }
    .reason-row .name { color: var(--ink); }
    .reason-row .count {
      background: var(--rejected-soft);
      color: var(--rejected);
      padding: 3px 10px;
      border-radius: 2px;
      font-family: "SF Mono", monospace;
      font-size: 12px;
      font-weight: 600;
    }

    .start-over {
      display: block;
      text-align: center;
      margin-top: 32px;
      color: var(--muted);
      font-size: 13px;
      text-decoration: none;
    }
    .start-over:hover { color: var(--brand); }

    .empty-state {
      text-align: center;
      padding: 32px 24px;
      color: var(--muted);
    }
    .empty-state h3 {
      font-family: "Iowan Old Style", Georgia, serif;
      font-weight: 400;
      color: var(--ink);
      font-size: 20px;
      margin-bottom: 8px;
    }

    .error-box {
      background: var(--rejected-soft);
      border: 1px solid var(--rejected);
      border-radius: 2px;
      padding: 16px 20px;
      margin-bottom: 20px;
      color: var(--rejected);
      font-size: 13px;
      white-space: pre-wrap;
      font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
      max-height: 320px;
      overflow-y: auto;
    }
    .error-box strong {
      display: block;
      margin-bottom: 8px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
  </style>
</head>
<body>

<div class="container">

  <header class="brand">
    <div class="brand-mark serif">S</div>
    <div class="brand-text">
      <div class="name">Sourcing</div>
      <div class="sub">Amazon FBM discovery</div>
    </div>
    {% if auth_enabled %}
    <div class="user-menu">
      <span class="user-name">{{ username }}</span>
      <a class="signout" href="/logout" title="Sign out">Sign out</a>
    </div>
    {% endif %}
  </header>

  <section class="hero">
    <h1>Find products worth <em>listing</em>.</h1>
    <p>Pick a supplier, choose how many products to analyze, and get back a filtered list with real profit numbers and Amazon competition data.</p>
  </section>

  <form id="runForm">
    <div class="run-card">
      <div class="run-body">

        <div class="fieldset">
          <div class="field">
            <label for="retailer">Supplier</label>
            <select id="retailer" name="retailer">
              {% for r in retailers %}
              <option value="{{ r.value }}">{{ r.label }} - {{ r.sub }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="field">
            <label for="limit">Products to analyze</label>
            <input type="number" id="limit" name="limit" value="25" min="1" max="500">
          </div>
        </div>

        <div class="fieldset">
          <div class="field">
            <label for="brands">Focus on brands (optional)</label>
            <input type="text" id="brands" placeholder='e.g., "Now Foods, Jarrow Formulas"'>
            <div class="field-hint">Comma-separated. Only products from these brands.</div>
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <div class="field-hint" style="padding-top: 8px;">
              For iHerb, the tool uses the brand listing with iHerb's own In-stock filter, then double-checks each product page.
            </div>
          </div>
        </div>

        <div class="field">
          <label>Approval criteria</label>
          <div class="preset-row">
            {% for key, p in presets.items() %}
            <button class="preset {% if key == 'balanced' %}selected{% endif %}"
                    data-preset="{{ key }}" type="button">
              <div class="preset-label">{{ p.label }}</div>
              <div class="preset-sub">{{ p.sub }}</div>
              <div class="preset-desc">{{ p.desc }}</div>
            </button>
            {% endfor %}
          </div>
        </div>

        <details class="advanced">
          <summary>Adjust individual filters</summary>
          <div class="advanced-body">

            <div class="adv-group">
              <label>Min profit ($)</label>
              <input type="number" id="min_profit" step="0.01" value="2">
            </div>
            <div class="adv-group">
              <label>Min margin (%)</label>
              <input type="number" id="min_margin" value="10">
            </div>
            <div class="adv-group">
              <label>Min ROI (%)</label>
              <input type="number" id="min_roi" value="15">
            </div>

            <div class="adv-group">
              <label>Min FBM sellers</label>
              <input type="number" id="min_fbm_sellers" value="4">
            </div>
            <div class="adv-group">
              <label>Min rating</label>
              <input type="number" id="min_rating" step="0.1" value="3.5">
            </div>
            <div class="adv-group">
              <label>Min reviews</label>
              <input type="number" id="min_reviews" value="0">
            </div>

            <div class="adv-group">
              <label>Min historical sellers</label>
              <input type="number" id="min_historical_sellers" value="0">
            </div>
            <div class="adv-group">
              <label>Max BSR (0 = none)</label>
              <input type="number" id="max_bsr" value="0">
            </div>
            <div class="adv-group">
              <label>Amazon fee (%)</label>
              <input type="number" id="fee_pct" value="15">
            </div>

            <div class="adv-check" style="grid-column: 1 / -1;">
              <input type="checkbox" id="require_rating">
              <label for="require_rating">Require rating (reject if no rating at all)</label>
            </div>
            <div class="adv-check" style="grid-column: 1 / -1;">
              <input type="checkbox" id="require_reviews">
              <label for="require_reviews">Require reviews (reject if no reviews at all)</label>
            </div>

          </div>
        </details>

      </div>

      <div class="run-footer">
        <div class="footnote">
          Out-of-stock supplier products are skipped automatically.
          Analysis takes roughly 20 seconds per product. Each product costs ~7 Keepa tokens.
        </div>
        <button type="submit" class="btn-run" id="runBtn">Start analysis</button>
      </div>
    </div>
  </form>

  <div class="progress-card" id="progressCard">
    <div class="stages">
      <div class="stage" data-stage="discovering">
        <div class="stage-num">1</div>
        <div class="stage-label">Discovering</div>
        <div class="stage-sub">Reading supplier catalog</div>
      </div>
      <div class="stage" data-stage="scraping">
        <div class="stage-num">2</div>
        <div class="stage-label">Scraping</div>
        <div class="stage-sub">Fetching product details</div>
      </div>
      <div class="stage" data-stage="matching">
        <div class="stage-num">3</div>
        <div class="stage-label">Matching</div>
        <div class="stage-sub">Looking up on Amazon</div>
      </div>
      <div class="stage" data-stage="finalizing">
        <div class="stage-num">4</div>
        <div class="stage-label">Filtering</div>
        <div class="stage-sub">Applying your rules</div>
      </div>
    </div>

    <div class="banner-warn" id="keepaExhaustedBanner">
      <strong>Keepa quota hit.</strong>
      Partial results were saved. Products still to check are marked
      <em>INCOMPLETE</em> in the Excel file — re-run in a few minutes once
      Keepa refills your token bucket.
    </div>

    <div class="progress-bar-wrap" id="progressBarWrap">
      <div class="progress-header">
        <span class="label" id="progressStageLabel">Working</span>
        <span class="counter" id="progressCounter"></span>
      </div>
      <div class="progress-bar">
        <div class="progress-bar-fill" id="progressBarFill"></div>
      </div>
      <div class="progress-detail">
        <span class="activity" id="progressActivity">&nbsp;</span>
        <span class="eta" id="progressEta"></span>
      </div>
    </div>

    <div class="progress-pills" id="progressPills"></div>

    <div class="log-view" id="logView"></div>
  </div>

  <div class="results-card" id="resultsCard">
    <div class="results-header">

      <div class="error-box" id="errorBox" style="display: none;">
        <strong>Something went wrong.</strong>
        <span id="errorMessage"></span>
      </div>

      <h2>Analysis <em>complete</em></h2>

      <div class="stat-row" id="statRow"></div>

      <div class="download-cta" id="downloadCta">
        <div class="txt">All data lives in the spreadsheet, including rejected products.</div>
        <a class="btn-download" id="downloadBtn" href="#" target="_blank">Download Excel</a>
      </div>
    </div>

    <div id="approvedSection" style="display: none;">
      <h3 class="section-title">Top approved products</h3>
      <p class="section-hint">Ranked by profit per unit. Full list in the Excel file.</p>
      <div class="approved-card" id="approvedList"></div>
    </div>

    <div id="reasonsSection" style="display: none;">
      <h3 class="section-title">Why others were skipped</h3>
      <p class="section-hint">Change filters and re-run to see if these come through.</p>
      <div class="reasons-card" id="reasonsList"></div>
    </div>

    <div id="emptyState" class="empty-state" style="display: none;">
      <h3>No approved products this run.</h3>
      <p>Try loosening your criteria or running a larger batch.</p>
    </div>

    <a href="#" class="start-over" onclick="location.reload(); return false;">Run another analysis</a>
  </div>

</div>

<script>
const PRESETS = {{ presets|tojson }};

document.querySelectorAll(".preset").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".preset").forEach(b => b.classList.remove("selected"));
    btn.classList.add("selected");
    applyPreset(btn.dataset.preset);
  });
});

function applyPreset(key) {
  const v = PRESETS[key].values;
  set("min_profit", v.min_profit);
  set("min_margin", Math.round(v.min_margin * 100));
  set("min_roi", Math.round(v.min_roi * 100));
  set("min_fbm_sellers", v.min_fbm_sellers);
  set("min_rating", v.min_rating);
  set("min_reviews", v.min_reviews);
  set("min_historical_sellers", v.min_historical_sellers);
  set("max_bsr", v.max_bsr);
  document.getElementById("require_rating").checked = v.require_rating;
  document.getElementById("require_reviews").checked = v.require_reviews;
}
function set(id, val) { document.getElementById(id).value = val; }

function buildConfig() {
  const val = id => parseFloat(document.getElementById(id).value) || 0;
  const chk = id => document.getElementById(id).checked;
  const maxBsr = val("max_bsr");
  return {
    fee_pct: val("fee_pct") / 100,
    min_profit: val("min_profit"),
    min_margin: val("min_margin") / 100,
    min_roi: val("min_roi") / 100,
    min_fbm_sellers: parseInt(val("min_fbm_sellers")) || 0,
    min_historical_sellers: parseInt(val("min_historical_sellers")) || 0,
    reject_if_amazon_on_listing: false,
    min_rating: val("min_rating"),
    require_rating: chk("require_rating"),
    min_reviews: parseInt(val("min_reviews")) || 0,
    require_reviews: chk("require_reviews"),
    max_bsr: maxBsr > 0 ? maxBsr : null,
  };
}

document.getElementById("runForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  btn.textContent = "Starting";

  document.getElementById("resultsCard").classList.remove("visible");
  document.getElementById("progressCard").classList.add("visible");
  document.getElementById("logView").textContent = "";
  document.querySelectorAll(".stage").forEach(s => s.classList.remove("done", "active"));
  document.getElementById("progressBarWrap").classList.remove("visible");
  document.getElementById("progressBarFill").style.width = "0%";
  document.getElementById("progressCounter").textContent = "";
  document.getElementById("progressActivity").textContent = "\u00A0";
  document.getElementById("progressEta").textContent = "";
  document.getElementById("progressPills").innerHTML = "";
  document.getElementById("keepaExhaustedBanner").classList.remove("visible");

  const config = buildConfig();

  const payload = {
    sitemap_url: document.getElementById("retailer").value,
    limit: parseInt(document.getElementById("limit").value),
    brands: document.getElementById("brands").value.trim(),
    random: true,
    no_robots: true,
    config: config,
  };

  try {
    const resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    document.getElementById("progressCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
    pollStatus(data.job_id);
  } catch (err) {
    showError(err.message);
    btn.disabled = false;
    btn.textContent = "Start analysis";
  }
});

const STAGE_ORDER = ["discovering", "scraping", "matching", "finalizing"];
const STAGE_LABELS = {
  discovering: "Discovering products",
  scraping:    "Scraping supplier",
  matching:    "Matching on Amazon",
  finalizing:  "Applying your rules",
};

function fmtEta(sec) {
  if (sec == null || sec < 0) return "";
  if (sec < 60) return "~" + sec + "s left";
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return "~" + m + "m " + (s ? s + "s" : "") + " left";
  const h = Math.floor(m / 60);
  return "~" + h + "h " + (m % 60) + "m left";
}

function renderProgressPills(data) {
  const pills = [];
  if (data.urls_discovered != null) {
    pills.push('<span class="pill">Discovered <strong>' +
      data.urls_discovered + '</strong> URLs</span>');
  }
  if (data.keepa_tokens_left != null) {
    const cls = data.keepa_tokens_left < 100 ? "pill warn" : "pill";
    pills.push('<span class="' + cls + '">Keepa tokens <strong>' +
      data.keepa_tokens_left + '</strong></span>');
  }
  document.getElementById("progressPills").innerHTML = pills.join("");
}

async function pollStatus(jobId) {
  try {
    const resp = await fetch("/api/status/" + jobId);
    const data = await resp.json();

    document.getElementById("logView").textContent = data.log_tail.join("\n");
    const logEl = document.getElementById("logView");
    logEl.scrollTop = logEl.scrollHeight;

    // Stage highlighting
    if (data.stage && STAGE_ORDER.includes(data.stage)) {
      const idx = STAGE_ORDER.indexOf(data.stage);
      STAGE_ORDER.forEach((s, i) => {
        const el = document.querySelector('.stage[data-stage="' + s + '"]');
        el.classList.remove("active", "done");
        if (i < idx) el.classList.add("done");
        else if (i === idx) el.classList.add("active");
      });
    }

    // Per-item progress bar
    const barWrap = document.getElementById("progressBarWrap");
    if (data.current != null && data.total != null && data.total > 0) {
      barWrap.classList.add("visible");
      const pct = Math.min(100, Math.round(100 * data.current / data.total));
      document.getElementById("progressBarFill").style.width = pct + "%";
      document.getElementById("progressCounter").textContent =
        data.current + " / " + data.total;
      document.getElementById("progressStageLabel").textContent =
        STAGE_LABELS[data.stage] || "Working";
      document.getElementById("progressEta").textContent =
        fmtEta(data.eta_seconds);
    }
    if (data.current_activity) {
      document.getElementById("progressActivity").textContent =
        data.current_activity;
    }

    renderProgressPills(data);

    if (data.keepa_exhausted) {
      document.getElementById("keepaExhaustedBanner").classList.add("visible");
    }

    if (data.status === "done") {
      STAGE_ORDER.forEach(s => {
        const el = document.querySelector('.stage[data-stage="' + s + '"]');
        el.classList.remove("active");
        el.classList.add("done");
      });
      document.getElementById("progressBarFill").style.width = "100%";
      document.getElementById("progressEta").textContent = "";
      setTimeout(() => showResults(jobId, data.summary), 400);
    } else if (data.status === "error") {
      showError(data.error || "Unknown error");
    } else {
      setTimeout(() => pollStatus(jobId), 1500);
    }
  } catch (err) {
    showError("Lost connection: " + err.message);
  }
}

function showResults(jobId, summary) {
  document.getElementById("progressCard").classList.remove("visible");
  const results = document.getElementById("resultsCard");
  results.classList.add("visible");

  const btn = document.getElementById("runBtn");
  btn.disabled = false;
  btn.textContent = "Start analysis";

  document.getElementById("errorBox").style.display = "none";
  document.getElementById("downloadCta").style.display = "";

  const byStatus = summary.by_status || {};
  const approved = byStatus["APPROVED"] || 0;
  const review = byStatus["REVIEW"] || 0;
  const rejected = byStatus["Rejected"] || 0;
  const total = summary.total || 0;

  const statRow = document.getElementById("statRow");
  statRow.innerHTML = "";
  addStat(statRow, approved, "Approved", "approved");
  if (review > 0) addStat(statRow, review, "Review", "review");
  addStat(statRow, rejected, "Skipped", "rejected");
  addStat(statRow, total, "Analyzed", "total");

  const approvedList = document.getElementById("approvedList");
  approvedList.innerHTML = "";
  if (summary.top_approved && summary.top_approved.length) {
    document.getElementById("approvedSection").style.display = "";
    document.getElementById("emptyState").style.display = "none";
    summary.top_approved.forEach(p => {
      const row = document.createElement("div");
      row.className = "approved-item";
      const asinLink = p.asin
        ? '<a href="https://www.amazon.com/dp/' + p.asin + '" target="_blank">' + p.asin + '</a>'
        : "";
      const supplierCost = p.supplier_cost ? "$" + p.supplier_cost.toFixed(2) : "";
      const amazonPrice = p.amazon_price ? "$" + p.amazon_price.toFixed(2) : "";
      const priceInfo = supplierCost && amazonPrice
        ? supplierCost + " to " + amazonPrice : "";
      const fbmInfo = p.fbm ? p.fbm + " FBM sellers" : "";
      const roiPct = p.roi ? (p.roi * 100).toFixed(0) + "% ROI" : "";
      row.innerHTML =
        '<div class="info">' +
          '<div class="title">' + escapeHtml(p.title || "Untitled") + '</div>' +
          '<div class="meta">' +
            asinLink +
            (priceInfo ? '<span>' + priceInfo + '</span>' : '') +
            (fbmInfo ? '<span>' + fbmInfo + '</span>' : '') +
          '</div>' +
        '</div>' +
        '<div>' +
          '<div class="profit">$' + (p.profit || 0).toFixed(2) + '</div>' +
          '<div class="roi">' + roiPct + '</div>' +
        '</div>';
      approvedList.appendChild(row);
    });
  } else {
    document.getElementById("approvedSection").style.display = "none";
    document.getElementById("emptyState").style.display = "";
  }

  const reasonsList = document.getElementById("reasonsList");
  reasonsList.innerHTML = "";
  if (summary.reject_reasons && summary.reject_reasons.length) {
    document.getElementById("reasonsSection").style.display = "";
    summary.reject_reasons.forEach(pair => {
      const reason = pair[0];
      const count = pair[1];
      const row = document.createElement("div");
      row.className = "reason-row";
      row.innerHTML = '<span class="name">' + escapeHtml(reason) + '</span>' +
                      '<span class="count">' + count + '</span>';
      reasonsList.appendChild(row);
    });
  } else {
    document.getElementById("reasonsSection").style.display = "none";
  }

  document.getElementById("downloadBtn").href = "/api/download/" + jobId;
}

function showError(msg) {
  document.getElementById("progressCard").classList.remove("visible");
  const results = document.getElementById("resultsCard");
  results.classList.add("visible");

  document.getElementById("errorMessage").textContent = msg;
  document.getElementById("errorBox").style.display = "";
  document.getElementById("statRow").innerHTML = "";
  document.getElementById("downloadCta").style.display = "none";
  document.getElementById("approvedSection").style.display = "none";
  document.getElementById("reasonsSection").style.display = "none";
  document.getElementById("emptyState").style.display = "none";

  const btn = document.getElementById("runBtn");
  btn.disabled = false;
  btn.textContent = "Start analysis";
}

function addStat(container, num, label, cls) {
  const d = document.createElement("div");
  d.className = "stat " + cls;
  d.innerHTML = '<div class="num">' + num + '</div><div class="lbl">' + label + '</div>';
  container.appendChild(d);
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

applyPreset("balanced");
</script>

</body>
</html>
"""


# ---------------------------------------------------------------- #
# Entrypoint                                                       #
# ---------------------------------------------------------------- #

def main() -> None:
    port = int(os.environ.get("PORT", 5000))
    url = f"http://127.0.0.1:{port}"
    print("=" * 60)
    print(f"  Sourcing UI running at:  {url}")
    print("=" * 60)
    print("  Press Ctrl+C to stop.")
    print()

    try:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
