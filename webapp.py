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
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file, abort

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
            "reject_if_amazon_on_listing": True, "max_bsr": 0,
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
            "reject_if_amazon_on_listing": True, "max_bsr": 0,
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
            "reject_if_amazon_on_listing": True, "max_bsr": 100000,
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
        }
    return job_id


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
        elif "fetching" in low:
            job["stage"] = "scraping"
        elif "keepa" in low and "lookup" in low:
            job["stage"] = "matching"
        elif "sourcing done" in low:
            job["stage"] = "finalizing"


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

@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        presets=PRESETS,
        retailers=RETAILERS,
    )


@app.route("/api/run", methods=["POST"])
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
        })


@app.route("/api/download/<job_id>")
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
            <label for="min_supplier_price">Min supplier price ($)</label>
            <input type="number" id="min_supplier_price" step="0.01" value="0" min="0">
            <div class="field-hint">iHerb: use 25 for free shipping. Zoro: usually 0.</div>
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <div class="field-hint" style="padding-top: 8px;">
              Tool oversamples the sitemap 3× and keeps only products above this price.
            </div>
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
              For iHerb, the tool pre-filters URLs by brand for speed.
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
              <input type="checkbox" id="reject_if_amazon_on_listing" checked>
              <label for="reject_if_amazon_on_listing">Reject if Amazon is on the listing</label>
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
  document.getElementById("reject_if_amazon_on_listing").checked = v.reject_if_amazon_on_listing;
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
    reject_if_amazon_on_listing: chk("reject_if_amazon_on_listing"),
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

  const config = buildConfig();
  config.min_supplier_price = parseFloat(document.getElementById("min_supplier_price").value) || 0;

  const payload = {
    sitemap_url: document.getElementById("retailer").value,
    limit: parseInt(document.getElementById("limit").value),
    min_supplier_price: config.min_supplier_price,
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

// Auto-suggest min price = $25 when iHerb is selected
document.getElementById("retailer").addEventListener("change", (e) => {
  const priceInput = document.getElementById("min_supplier_price");
  if (e.target.value.includes("iherb")) {
    if (parseFloat(priceInput.value) === 0) priceInput.value = "25";
  } else {
    if (parseFloat(priceInput.value) === 25) priceInput.value = "0";
  }
});

const STAGE_ORDER = ["discovering", "scraping", "matching", "finalizing"];

async function pollStatus(jobId) {
  try {
    const resp = await fetch("/api/status/" + jobId);
    const data = await resp.json();

    document.getElementById("logView").textContent = data.log_tail.join("\n");
    const logEl = document.getElementById("logView");
    logEl.scrollTop = logEl.scrollHeight;

    if (data.stage && STAGE_ORDER.includes(data.stage)) {
      const idx = STAGE_ORDER.indexOf(data.stage);
      STAGE_ORDER.forEach((s, i) => {
        const el = document.querySelector('.stage[data-stage="' + s + '"]');
        el.classList.remove("active", "done");
        if (i < idx) el.classList.add("done");
        else if (i === idx) el.classList.add("active");
      });
    }

    if (data.status === "done") {
      STAGE_ORDER.forEach(s => {
        const el = document.querySelector('.stage[data-stage="' + s + '"]');
        el.classList.remove("active");
        el.classList.add("done");
      });
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
