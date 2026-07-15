# dd-logo-scraper

Upload a school directory sheet (`.xlsx` / `.csv`); get it back with three columns appended:

| Column | Source |
|---|---|
| `gofan logo url` | GoFan's logo for the matched school |
| `gofan url` | `https://gofan.co/app/school/{huddleId}` |
| `official website logo` | logo scraped from the row's `Official Website` |

Rows are matched to GoFan by **name + city + state**, with city/state parsed out of the `Address` column.

FastAPI + Scrapy, deployed on Render. Pairs with the `dimedropper-logo-frontend` Next.js app.

---

## How it works

```
POST /jobs (xlsx)  ->  api.py  ->  subprocess: worker.py <job_dir>
                                      |
                                      +-- 1. gofan_search spider   (JSON API, no browser)
                                      +-- 2. website_logo spider   (HTTP -> Playwright fallback)
                                      +-- 3. sheet_io writes enriched.xlsx + enriched.csv
```

One job = one subprocess, because Scrapy/Twisted allows a single reactor per process and it cannot be restarted. Both spiders run inside **one** `CrawlerProcess`, chained via deferreds.

### GoFan matching

GoFan exposes a search endpoint (undocumented, found by watching the site's own network traffic):

```
GET https://api.gofan.co/v2/schools/search?q=<name>&limit=20
-> [{huddleId, name, city, state, zipCode, logoUrl, industryCode}]
```

`limit` is **required** — omit it and you get an HTTP 500.

Two things make this non-trivial, both handled in `gofan_match.py`:

**Names don't match.** GoFan uses its own names, so searching the official district name often returns *zero* results. Queries are progressively simplified until one lands:

| Sheet | GoFan |
|---|---|
| Julia Landon College Preparatory Middle School | Landon Middle School |
| Duncan U. Fletcher Middle School | Duncan Fletcher Middle School |
| Darnell-Cookman School of the Medical Arts | Darnell-Cookman Middle School |
| Mayport Coastal Sciences Middle School | Mayport Middle School |

The full name alone matches 15/26 on the Duval sheet; the ladder reaches 24/26.

**Names collide across states.** "Arlington Middle School" is both `TN73539` (Tennessee) and `FL25617` (Jacksonville FL). Every candidate is gated on exact `state`, then `zipCode` or `city`.

> Do **not** switch this to the bulk catalog (`GET /v2/schools?page=&size=`). It's a
> partial, high-school-biased index: only 120 of its 25,728 entries are middle
> schools, zero Duval middle schools appear in it, and `TN73539` is missing from it
> despite existing.

### Website logos

`www.duvalschools.org` (Apptegy CMS) serves a JavaScript **"Client Challenge"** to non-browser clients — every path on the domain, including `/favicon.ico`, returns the same ~3 KB challenge page. This is bot detection, **not** a geo-block; US datacenter IPs get it too.

So `website_logo` tries a plain fetch first (cheap, correct for ordinary sites) and only escalates rows that come back challenged or logo-less to **scrapy-playwright**. Rendered, the logo is the header's home-link image:

```
https://cmsv2-assets.apptegy.net/uploads/24463/logo/27276/Mandarin_Middle_School_Logo.png
```

The Apptegy CDN itself is not challenged — only the HTML pages are.

---

## Input format

The header row is **detected**, not assumed. The Duval sheet has its header on row 6 with five preamble rows above it. Required columns: `School Name`, `Address`. Optional: `Official Website` (no website → blank logo).

`.xlsx` output is produced by editing a **copy of the original workbook**, so preamble rows, styling, Excel Table ranges and other sheets survive. Re-running is idempotent: existing columns are overwritten, not duplicated.

---

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium

.venv/bin/uvicorn api:app --reload --port 8000
```

Run one job without the API:

```bash
mkdir -p /tmp/job && cp your_sheet.xlsx /tmp/job/input.xlsx
.venv/bin/python worker.py /tmp/job
```

`SKIP_WEBSITE_LOGO=1` skips the browser stage (fast; GoFan columns only).

### If `duvalschools.org` won't resolve locally

Some ISP/router resolvers `SERVFAIL` on it (`nslookup` fails while `dig @8.8.8.8` works). That's local, not the site, and does not affect Render. Either point your resolver at `8.8.8.8`, or map the host for the browser stage:

```bash
PLAYWRIGHT_HOST_RESOLVER_RULES="MAP *.duvalschools.org 151.101.194.37" \
  .venv/bin/python worker.py /tmp/job
```

The wildcard matters — Chromium ignores a bare-host `MAP` rule here. This is a dev-only escape hatch; leave it unset in production.

---

## API

| Endpoint | Notes |
|---|---|
| `POST /jobs` | multipart `file` → `{job_id, status, row_count}`. 422 on a bad sheet, 429 if a job is already running |
| `GET /jobs/{id}` | `{status, progress:{stage,done,total}, counts, error}` |
| `GET /jobs/{id}/results` | JSON rows; 409 until done |
| `GET /jobs/{id}/download?format=xlsx\|csv` | 409 until done |
| `DELETE /jobs/{id}` | kill + clean up |
| `GET /health` | Render health check |

---

## Deploy (Render)

`render.yaml` is a Docker blueprint. Two constraints are load-bearing:

- **`plan: standard`** — Chromium is ~400 MB resident; starter (512 MB) OOM-kills it.
- **`MAX_CONCURRENT_JOBS=1` and one uvicorn worker** — `JOBS` is an in-memory dict holding live `Popen` handles, so a second worker wouldn't see the first's jobs. Jobs also don't survive a restart.

Set `FRONTEND_ORIGIN` to the Vercel domain.

---

## Known limits

- **24/26 on the Duval sheet.** GRASP Academy and Jacksonville STEM Academy aren't on GoFan; they're left blank rather than mismatched.
- **~6 of 24 GoFan logos are placeholders** — GoFan returns a generic grey `gofan-logo-black.png` for schools that never uploaded one. Written as-is (`is_placeholder_logo()` can detect them: real logos are namespaced `/logo/{huddleId}/...`).
- **`/v2/schools/search` is undocumented** and could change. A shape change logs a warning rather than silently writing blanks.
- Logo URLs frequently contain spaces (12 of 24 on this sheet) and are percent-encoded on write. Raw, they 404.
