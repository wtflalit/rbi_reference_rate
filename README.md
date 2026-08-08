# Daily FBIL / RBI Reference Rates → Excel → Google Drive

Scrapes the official INR reference rates (USD, EUR, GBP, JPY), writes them to a
formatted `.xlsx`, appends them to a cumulative history workbook, and uploads
both to a Google Drive folder — automatically, every weekday, via GitHub
Actions.

---

## Read this first

**The scrapers have not been run against the live sites.** Neither
`fbil.org.in` nor `rbi.org.in` publishes a documented API, so both scrapers
parse HTML that these sites can change without notice. The parsing logic is
deliberately layout-tolerant and is covered by tests against realistic markup,
but the only way to know it works from your network today is to run:

```bash
python scripts/probe_sources.py --dump-html
```

Do that **before** you wire up the schedule. It prints, per source, whether the
site was reachable, what rates it parsed, and what date it thinks they belong
to — and saves the raw HTML to `debug/` so a layout change is a two-minute fix
rather than a mystery. [Fixing a broken scraper](#when-a-scraper-breaks) walks
through it.

---

## How it works

```
      ┌── fbil ────────────┐   official benchmark, tried first
IST → ├── rbi ─────────────┤ → first non-empty result wins
date  └── frankfurter ─────┘   JSON mirror, last resort
                │
                ├── reference_rates_YYYY-MM-DD.xlsx   (daily snapshot)
                └── reference_rates_master.xlsx       (full history)
                            │
                            └── Google Drive folder (replace in place)
```

**Why three sources.** FBIL took over reference-rate computation from the RBI
in July 2018 and is the authoritative publisher, so it is tried first. RBI
re-publishes FBIL's numbers and is a genuine fallback, though its legacy
archive is an ASP.NET WebForms page that needs a `__VIEWSTATE` postback and can
throttle non-Indian IPs — which is exactly what GitHub's US-hosted runners are.
Frankfurter is a free JSON mirror carrying an FBIL series; it exists so a
markup change at both Indian sites does not take the pipeline down.

**One caveat on Frankfurter.** If its FBIL endpoint answers, rows are tagged
`frankfurter-fbil` and the numbers are the official fix. If only its default
endpoint answers, the numbers come from the ECB fixing instead — close to, but
not the same as, the Indian benchmark. Those rows are tagged `frankfurter-ecb`
and flagged as `INDICATIVE` on the workbook's Metadata sheet. Pass
`--require-official` to refuse them outright; use that flag if this data feeds
accounting, invoicing, or anything contractual.

---

## Project layout

```
.
├── .github/workflows/
│   ├── daily-rates.yml       # scheduled + manual run
│   └── tests.yml             # CI on push/PR
├── config/
│   └── holidays.yml          # Mumbai bank holidays — see below
├── scripts/
│   └── probe_sources.py      # diagnostic; run this when things break
├── src/
│   ├── main.py               # orchestrator + CLI
│   ├── models.py             # Rate dataclass, error types
│   ├── calendar_utils.py     # weekend / holiday / IST logic
│   ├── excel_writer.py       # daily workbook + history merge
│   ├── drive_client.py       # upload-or-replace
│   └── sources/
│       ├── base.py           # HTTP, retries, shared parsing helpers
│       ├── html_parsing.py   # layout-tolerant table extraction
│       ├── fbil.py           # primary
│       ├── rbi.py            # fallback
│       └── frankfurter.py    # last resort
├── tests/test_pipeline.py    # 28 offline tests, no network or credentials
├── requirements.txt
└── .env.example
```

---

## Local setup

```bash
git clone <your-repo-url>
cd rbi-fbil-rates

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then fill in GDRIVE_FOLDER_ID
```

Check the sources are reachable, then do a full offline run:

```bash
python scripts/probe_sources.py
python -m src.main --no-upload     # writes to output/, touches no cloud
```

Run the tests any time — they need no network and no credentials:

```bash
pytest -q
```

---

## Google Cloud setup

You need a **service account**, not the interactive OAuth flow. OAuth needs a
browser to click a consent screen; a GitHub runner has neither. (An OAuth path
is still included for local use — see [below](#optional-oauth-for-local-runs).)

### 1. Create the project and enable the API

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (e.g. `rbi-rates-automation`), or select an existing one.
3. **APIs & Services → Library** → search **Google Drive API** → **Enable**.

### 2. Create the service account

1. **APIs & Services → Credentials → Create Credentials → Service account**.
2. Name it something recognisable, e.g. `rates-uploader`. Skip the optional
   role and user-access steps — it needs no project-level IAM role, only Drive
   file permissions, which you grant in step 4.
3. Open the new service account → **Keys** → **Add key → Create new key →
   JSON**. A `.json` file downloads. **This is a credential — treat it like a
   password.** It is already in `.gitignore`; keep it that way.
4. Copy the service account's email address. It looks like
   `rates-uploader@rbi-rates-automation.iam.gserviceaccount.com`.

### 3. Create the Drive folder

In Google Drive, create the destination folder and copy its ID from the URL:

```
https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
                                       └──────── this is GDRIVE_FOLDER_ID ────┘
```

### 4. Share the folder with the service account

Right-click the folder → **Share** → paste the service account email → set the
role to **Editor** → Share.

> **Do not skip this.** A service account is a separate identity from your
> Google account. Without the share it cannot see the folder, and the run fails
> at `--check-drive` with a 404.

### 5. Verify locally

```bash
export GDRIVE_FOLDER_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz
export GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/key.json

python -m src.main --check-drive     # confirms creds + folder access
python -m src.main                   # full run: scrape, build, upload
```

### The service-account storage-quota trap

Service accounts have **no Drive storage quota of their own**. If the service
account ends up *owning* a file, the upload fails with
`storageQuotaExceeded` — which is a confusing error, because the folder has
plenty of space.

Two fixes, both supported:

- **Simple:** keep the folder in your own My Drive and share it with the
  service account (step 4 above). Files created inside a folder you own are
  charged to *your* quota. No Workspace subscription needed. This is what most
  people want.
- **Shared Drive:** move the folder into a Shared Drive and set
  `GDRIVE_SHARED_DRIVE_ID`. Requires Google Workspace. Better if several people
  need ownership of the output.

The code raises a plain-English error pointing back here if it hits this.

### Optional: OAuth for local runs

If you would rather run against your own account locally without minting a
service account, drop a `credentials.json` (Desktop app OAuth client) in the
project root and run the script. A browser opens once, and the resulting
`token.json` is cached for later runs. Both files are gitignored.

Be aware: while the OAuth consent screen is in **Testing** mode, Google expires
refresh tokens after 7 days — fine for local use, useless for a daily job.
That expiry is the reason CI uses a service account.

---

## GitHub Actions setup

### Secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `GDRIVE_SERVICE_ACCOUNT_JSON` | The **entire contents** of the service-account JSON key file — open it in a text editor and paste everything, braces included. |
| `GDRIVE_FOLDER_ID` | The folder ID from the Drive URL. |

### Variables (optional)

**Same page → Variables tab**

| Variable | When you need it |
|---|---|
| `GDRIVE_SHARED_DRIVE_ID` | Only if the folder lives in a Shared Drive. |

### Schedule

```yaml
- cron: "0 9 * * 1-5"     # 09:00 UTC = 14:30 IST, Mon–Fri
```

FBIL publishes around 13:30 IST, so this leaves an hour of headroom. GitHub's
scheduled runs are best-effort and can be delayed by several minutes during
peak load, which is harmless here — the script re-checks the calendar and the
publication window itself.

> Note: GitHub disables scheduled workflows in repositories with no activity
> for 60 days. A single commit re-enables them.

### Manual trigger

**Actions → Daily Reference Rates → Run workflow.** Inputs: `date`, `sources`,
`force`, `no_upload` — enough to backfill a specific day or test a single
source without editing anything.

---

## Idempotency

The whole design turns on one rule: **a no-op and a success must look the
same.** Otherwise every public holiday produces a red build, and red builds you
expect are red builds you stop reading.

| Situation | Behaviour | Exit |
|---|---|---|
| Saturday or Sunday | Skipped before any network call | `0` |
| Date listed in `holidays.yml` | Skipped before any network call | `0` |
| Unlisted holiday — nothing published | Sources queried, nothing found, logged | `0` |
| Run before 13:30 IST | Logged as "not published yet" | `0` |
| File for today already in Drive | Updated in place — same file ID, no duplicate | `0` |
| Same date scraped twice | History row overwritten, not appended | `0` |
| One source down | Falls through to the next | `0` |
| **All sources down** | Real outage — loud failure | `1` |
| **Drive rejects the upload** | Real failure | `1` |

Re-uploads use `files.update`, not delete-and-recreate, so the Drive file ID
stays stable and any shared links or downstream Sheets references keep working.

---

## The holiday file

`config/holidays.yml` ships with **only the fixed-date national holidays**
pre-filled. The movable ones — Holi, Diwali, Id, Dussehra, Gudi Padwa, Muharram
— shift every year against the Gregorian calendar, and guessing them would put
wrong dates in your config. Fill them in once a year from the RBI's *Holidays
under Negotiable Instruments Act* notification for Maharashtra.

Getting this wrong is cheap by design: the file only skips pointless network
calls. A missing holiday means the script runs, finds nothing published, logs
it, and exits `0` — the data-driven path catches what the list misses.

---

## Historical backfill

The daily job only ever fetches one date, so history accumulates going
forward. To load everything FBIL has published since it took over in July
2018, run the backfill once:

```bash
python scripts/backfill.py --dry-run      # check coverage first
python scripts/backfill.py                # 2018-07-01 -> today
```

Or from GitHub: **Actions → Historical Backfill → Run workflow**.

It does *not* scrape. There are ~2,100 business days in that range, and
hitting fbil.org.in 2,100 times would be slow, fragile and rude — the kind of
traffic that gets an IP blocked and takes the daily job down with it. Instead
it uses Frankfurter's time-series endpoint, which returns a whole date range
per request: about 32 requests total.

Measured on a full 2018→2026 run: 2,115 business days, 8,460 rates, ~40
seconds, 26 MB of output (17 MB Excel, 8.4 MB PDF, 360 KB master).

| Flag | Effect |
|---|---|
| `--start` / `--end` | Date range. Default `2018-07-01` → today. |
| `--no-per-date` | Master workbook only — skips the ~4,200 per-date files. |
| `--no-pdf` | Excel only. |
| `--require-official` | Abort rather than backfill years of indicative ECB data. |
| `--dry-run` | Report coverage, write nothing. |

**Pre-2018 is not covered.** Those rates are RBI's own, from before FBIL
existed, and are only available through RBI's legacy ASP.NET archive. That
path exists in `src/sources/rbi.py` but expects date-range postbacks I could
not verify — treat it as a starting point, not a working feature.

Re-running is safe: the master workbook deduplicates on (date, pair), and
per-date files are simply overwritten.

---

## Output

Excel and PDF land in sibling folders so each format can be synced, shared or
archived independently:

```
data/
├── excel/reference_rates_YYYY-MM-DD.xlsx   one per business day
├── pdf/reference_rates_YYYY-MM-DD.pdf      one per business day
└── reference_rates_master.xlsx             full history, single file
```

The PDF is a single page per date: the four pairs in a formatted table, plus
source, retrieval timestamp, and a prominent warning if the data is
indicative rather than the official fix. Use `--no-pdf` to skip it.

**`reference_rates_YYYY-MM-DD.xlsx`** — the daily snapshot.

- *Reference Rates*: one row per pair, with source and UTC retrieval time.
- *Metadata*: rate date, pairs captured, pairs missing, provenance.

**`reference_rates_master.xlsx`** — the running history.

- *History*: long format, one row per (date, pair). Deduplicated on that key.
- *Wide*: one row per date, one column per pair — the shape you actually chart.

JPY is quoted **per 100 yen**, following FBIL's convention. The `Unit` column
makes that explicit so nobody silently compares a per-100 figure against a
per-1 figure six months from now.

---

## When a scraper breaks

```bash
python scripts/probe_sources.py --date 2026-08-07 --dump-html
```

Read the output before changing any code:

| What you see | What it means | Fix |
|---|---|---|
| `UNREACHABLE` / connection errors | Network, WAF, or geo-block | Try from an Indian IP; if only CI fails, reorder `RATE_SOURCES` to put `frankfurter` first |
| Page saved, `rates found: {}` | Markup moved | See below |
| `rates found: {...}`, `date found: None` | Date caption moved | Add the new pattern to `extract_rate_date` in `src/sources/html_parsing.py` |
| Rates and date both found, date ≠ requested | Correct behaviour — no fix published yet | Nothing to do |

**If the markup moved:** open the saved `debug/*.html`, find the table holding
the rates, and check what the currency cell now says. Parsing scans every table
for a row containing a currency name plus a number inside a plausible band, so
it survives column reordering and renamed headers on its own. It only needs
help when the currency *label* changes — add the new wording to
`CURRENCY_PATTERNS` in `src/sources/html_parsing.py`.

**If the archive postback stopped working:** open the page in a browser with
DevTools' Network tab recording, submit the date filter, and copy the form-data
key names from the request into `ARCHIVE_FORM_FIELDS` (FBIL) or
`LEGACY_FORM_FIELDS` (RBI). ASP.NET control IDs change whenever someone edits
the page; nothing else needs touching.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `File not found: <folder id>` | Folder not shared with the service account | Share it as **Editor** (setup step 4) |
| `storageQuotaExceeded` | Service account owns the file | See [the quota trap](#the-service-account-storage-quota-trap) |
| `Secret ... is not valid JSON` | Partial paste | Re-copy the *whole* key file, braces included |
| Rates found but all `frankfurter-ecb` | Both Indian sites unreachable | Indicative data — see the caveat above; add `--require-official` to fail instead |
| Workflow never fires | 60-day inactivity disable | Push any commit |
| Empty `output/` after a green run | Weekend, holiday, or nothing published | Working as intended — check the log line |

---

## CLI reference

```
python -m src.main [options]

  --date YYYY-MM-DD      Business date. Default: today in IST.
  --sources a,b,c        Source chain, tried in order. Default: fbil,rbi,frankfurter
  --output-dir PATH      Where workbooks are written. Default: output
  --holidays PATH        Holiday YAML. Default: config/holidays.yml
  --master-filename NAME History workbook name. Default: reference_rates_master.xlsx
  --no-upload            Build locally, skip Drive entirely.
  --no-master            Daily file only, no history workbook.
  --force                Ignore weekend/holiday gating.
  --require-official     Fail rather than accept indicative (ECB) rates.
  --check-drive          Verify credentials and folder access, then exit.
  --log-level LEVEL      DEBUG | INFO | WARNING | ERROR
```

---

## Licence and data usage

The code is yours to do as you like with. The **data** is not: FBIL reference
rates are published under FBIL's own terms, and redistribution or commercial
use may require their permission. Check
[fbil.org.in](https://www.fbil.org.in/) before republishing anything scraped
here. Scrape politely — once a day is plenty, which is what this is built for.
