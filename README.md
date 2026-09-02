# Amazon FBM Sourcing Tool

Automated product sourcing tool for Amazon FBM (Fulfilled-by-Merchant) arbitrage.
Scrapes supplier retailer catalogs (iHerb, Zoro), matches products to Amazon
listings via the Keepa API, applies configurable sourcing rules (profit, margin,
ROI, seller counts, ratings, BSR), and outputs approved products to an Excel
spreadsheet.

---

## Features

- **Multi-retailer scraping** — iHerb (US pricing) and Zoro (US)
- **Keepa API integration** — accurate Amazon data via UPC lookup: ASIN, live
  FBM/FBA seller counts, BSR, ratings, review counts, Buy Box price
- **Configurable sourcing rules** — profit, margin, ROI, FBM sellers, ratings,
  BSR, historical sellers, Amazon-on-listing rejection
- **Smart filtering** — $25 minimum price for iHerb (free shipping threshold),
  brand-targeted URL pre-filtering
- **Shipping cost handling** — configurable per-unit extra cost buffer
- **Web-based UI** — non-technical users operate via any browser
- **Excel output** — clean formatted xlsx with all product data and rejection
  reasons
- **CLI and web interfaces** — both modes supported

---

## System Requirements

- **Python 3.10+** (tested on 3.12)
- **curl** (pre-installed on Ubuntu; included on Windows 10+)
- **Keepa API key** (Starter plan or above — https://keepa.com/#!api)
- Internet connection

---

## Installation

### 1. Clone or download the repository

```bash
git clone https://github.com/softonic-tech/amazon-automation.git
cd amazon-automation
```

### 2. Create a Python virtual environment

**On Linux / Mac:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**On Windows PowerShell:**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set your Keepa API key

**On Linux / Mac (current session):**
```bash
export KEEPA_API_KEY="your_key_here"
```

**On Linux / Mac (permanent — add to `~/.bashrc`):**
```bash
echo 'export KEEPA_API_KEY="your_key_here"' >> ~/.bashrc
source ~/.bashrc
```

**On Windows PowerShell (current session):**
```powershell
$env:KEEPA_API_KEY = "your_key_here"
```

**On Windows PowerShell (permanent):**
```powershell
[Environment]::SetEnvironmentVariable("KEEPA_API_KEY", "your_key_here", "User")
```

---

## Running the Web UI (Recommended for Non-Technical Users)

```bash
python webapp.py
```

Open your browser to `http://127.0.0.1:5000` and:

1. Select a supplier (iHerb or Zoro)
2. Set batch size (number of products to analyze)
3. Optionally set a minimum supplier price (auto-suggests $25 for iHerb)
4. Optionally add brands to focus on
5. Pick a preset (Balanced, Discovery, or Amazon-Safe)
6. Click **Start Analysis**
7. Wait for the batch to complete (~15 seconds per product)
8. View the summary dashboard and download the Excel file

---

## Running from Command Line

### Zoro batch

```bash
python scraper.py \
    --sitemap https://www.zoro.com \
    --limit 25 --random --sourcing \
    --out zoro.xlsx
```

### iHerb batch (US pricing, $25 minimum, brand-targeted)

```bash
python scraper.py \
    --sitemap https://www.iherb.com \
    --limit 25 --random --no-robots --sourcing \
    --min-supplier-price 25 \
    --brands "Now Foods, Jarrow Formulas" \
    --out iherb.xlsx
```

### Analyze a single product URL

```bash
python scraper.py \
    --url "https://www.iherb.com/pr/PRODUCT-URL-HERE" \
    --no-robots --sourcing \
    --out single_product.xlsx
```

### Re-analyze existing scraped JSON

```bash
python scraper.py \
    --from-scraped iherb_batch.json \
    --sourcing \
    --out iherb_reanalyzed.xlsx
```

---

## Configuration

Sourcing rules are configurable via a JSON file. Copy the example and adjust:

```bash
cp sourcing_config.example.json sourcing_config.json
```

Then edit `sourcing_config.json`:

```json
{
  "fee_pct": 0.15,
  "extra_cost": 0.0,
  "min_supplier_price": 0.0,

  "min_profit": 0.01,
  "min_margin": 0.10,
  "min_roi": 0.15,

  "min_fbm_sellers": 4,
  "max_fbm_sellers": null,
  "reject_if_amazon_on_listing": true,

  "min_rating": 3.5,
  "require_rating": true,
  "min_reviews": 0,
  "require_reviews": false,

  "max_bsr": null
}
```

### Configuration options

| Field | Meaning |
|---|---|
| `fee_pct` | Amazon referral fee percentage (default `0.15` = 15%) |
| `extra_cost` | Per-unit shipping/prep buffer in USD (deducted from profit) |
| `min_supplier_price` | Skip supplier products below this price (USD) |
| `min_profit` | Minimum profit per unit in USD |
| `min_margin` | Minimum margin (profit / sell price), e.g. `0.10` = 10% |
| `min_roi` | Minimum ROI (profit / cost), e.g. `0.15` = 15% |
| `min_fbm_sellers` | Minimum FBM sellers on the listing |
| `max_fbm_sellers` | Maximum FBM sellers (reject listings with too many) |
| `reject_if_amazon_on_listing` | Reject if Amazon itself is a seller |
| `min_rating` | Minimum Amazon rating (0.0 - 5.0) |
| `require_rating` | Reject if no rating available |
| `min_reviews` | Minimum review count |
| `require_reviews` | Reject if no reviews |
| `max_bsr` | Maximum Best Sellers Rank (higher = slower velocity) |

To use a custom config on the command line:
```bash
python scraper.py --sitemap https://www.iherb.com --limit 25 --random \
    --sourcing --config sourcing_config.json --out iherb.xlsx
```

---

## Profit Calculation

The tool calculates profit using this formula:

```
Profit = Amazon Sell Price − (Sell Price × fee_pct) − Supplier Cost − extra_cost
```

Where:
- `Amazon Sell Price` = current Buy Box price from Keepa
- `fee_pct` = Amazon referral fee (default 15%)
- `Supplier Cost` = scraped price from iHerb or Zoro
- `extra_cost` = configurable per-unit shipping/prep cost

`Margin` = `Profit / Sell Price` — percentage of sale that is profit
`ROI` = `Profit / Cost` — return per dollar invested

---

## Deployment to a VPS

The tool is designed to run on any Linux VPS with Python 3.10+.

### Basic deployment

```bash
# On the VPS
git clone https://github.com/softonic-tech/amazon-automation.git
cd amazon-automation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export KEEPA_API_KEY="your_key_here"
python webapp.py
```

Access at `http://YOUR_VPS_IP:5000`

### Production deployment (recommended)

For long-running production use, run behind a process manager and reverse proxy:

- **systemd** — auto-restart on crash and boot
- **gunicorn** — production WSGI server (1 worker; the app uses in-memory job
  tracking so do NOT scale workers)
- **nginx** — reverse proxy for HTTPS and clean URLs

---

## Sourcing Presets

The web UI ships with three presets:

- **Balanced** — Standard sourcing rules matching typical FBM arbitrage criteria
  (≥4 FBM sellers, ≥3.5 rating, 10% margin, 15% ROI)
- **Discovery** — Loose rules to surface more products for evaluation
- **Amazon-Safe** — Strict rules for competitive-listing safety (≥50 reviews,
  ≥4.0 rating, healthy BSR, 15% margin)

Custom presets can be created by editing `sourcing_config.json` or supplying
individual CLI flags.

---

## Anti-Bot Notes

Some retailer websites (iHerb, Walmart, Tractor Supply) use Cloudflare and
Akamai bot protection. Access from datacenter/VPS IP addresses can return 403
errors. Solutions:

1. **Residential proxy** (recommended) — Use a US residential proxy service
   like IPRoyal (~$5-15/month). Configure via `HTTP_PROXY_URL` environment
   variable.
2. **Local execution** — Running the tool from a home internet connection
   avoids the datacenter-IP problem for testing purposes.

---

## Adding a New Retailer

To add a new supplier retailer (e.g., Walmart, Home Depot):

1. Add URL patterns to `sitemap.py` for the new retailer
2. Add product-extraction logic to `scraper.py` (based on the retailer's
   HTML structure or JSON-LD)
3. Test with a single product URL first (`--url` flag)
4. Then test with the sitemap batch

Each retailer typically requires 2-3 days of engineering due to different
anti-bot systems and page structures.

---

## Project Files

| File | Purpose |
|---|---|
| `scraper.py` | Main CLI entry point and orchestrator |
| `webapp.py` | Flask web UI |
| `sourcing.py` | Sourcing rules engine and Excel output |
| `keepa.py` | Keepa API client for Amazon data |
| `sitemap.py` | Sitemap crawler with per-host URL patterns |
| `amazon.py` | Fallback Amazon web scraper (used when no Keepa key) |
| `curl_client.py` | HTTP client using system curl (bypasses Cloudflare) |
| `browser.py` | Playwright browser client (rarely needed) |
| `test_keepa.py` | Diagnostic — verifies Keepa API connectivity |
| `check_iherb_jsonld.py` | Diagnostic — inspects iHerb product data |
| `analyze_rejections.py` | Diagnostic — summarizes rejection reasons in an xlsx |
| `sourcing_config.example.json` | Example sourcing configuration |
| `requirements.txt` | Python dependencies |
| `.gitignore` | Excludes venv, test outputs, secrets |

---

## License

Delivered as work-for-hire. Client owns the source code.
