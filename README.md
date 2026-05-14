# tesla-toolbox-signals

Download Tesla **toolbox.tesla.com** datatank signals for a VIN and time range.

The tool drives your **actual installed browser** (Brave / Chrome / Edge) via
Playwright, using a persistent profile so the captcha state survives across
runs:

1. Auto-detects your installed browser (Brave > Chrome > Edge > bundled Chromium).
2. Opens a window pointing at `toolbox.tesla.com` using a profile dir under
   `state/browser-profile/`.
3. You log in once (Tesla SSO + captcha) and press ENTER in the terminal.
4. The profile is reused on subsequent runs — no captcha, no SSO.
5. All API calls go through that same browser, so they ride the real browser
   TLS/HTTP fingerprint and pass Akamai bot detection cleanly.

Then it:

- Lists every signal for the VIN/time range
  (`GET /api/v3/datatank/signals?key=vin:<VIN>&start=&end=&includeAudience=true&expand=true`).
- Fetches each signal individually with `filter=sig_name='<NAME>'`.
- Writes one JSON file per signal under
  `out/<VIN>/<startUTC>_<endUTC>/<sig_name>.json`, plus `_catalog.json` with
  all signal names.

## Requirements

- Python 3.10+
- A Tesla toolbox account with access to the target VIN

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
python -m src.main \
  --vin <YOUR_VIN> \
  --start <START_MS_OR_ISO> \
  --end   <END_MS_OR_ISO>
```

`--start` / `--end` accept either millisecond epoch values or ISO-8601
timestamps (e.g. `2026-01-01T00:00:00Z`).

First run pops a Chromium window — finish Tesla SSO, wait for the toolbox UI
to appear, then come back to the terminal and press ENTER. Subsequent runs
reuse the saved session and skip the prompt entirely.

### Useful flags

| Flag | Purpose |
| --- | --- |
| `--concurrency N` | Parallel signal fetches per batch (default 10; ~5–10x faster than serial) |
| `--login` | Ignore the cached profile and force a fresh login prompt |
| `--channel chrome\|chrome-beta\|msedge\|…` | Force a specific Playwright channel |
| `--executable-path PATH` | Use a specific browser binary (e.g. Brave) |
| `--overwrite` | Re-fetch signals whose JSON already exists |
| `--only NAME [NAME ...]` | Skip the catalog and fetch just these signals |
| `--dry-run` | List signals and write `_catalog.json` only |
| `--out-dir PATH` | Override output base directory |
| `--profile-dir PATH` | Override the persistent browser-profile directory |

### Output layout

```
out/
  <VIN>/
    <startUTC>_<endUTC>/
      _catalog.json
      BMS_brickVoltageMin.json
      BMS_minBusVoltage.json
      ...
```

Each per-signal file is the raw API response, e.g.:

```json
{
  "BMS_brickVoltageMin": {
    "metadata": { "...": "..." },
    "timestamps": [<ms>, ...],
    "sig_value":  [<value>, ...],
    "sig_text":   ["", ...],
    "is_sna":     [false, ...]
  }
}
```

## Notes

- The browser profile lives in `state/browser-profile/` (gitignored). Delete
  it (or pass `--login`) to force a fresh SSO + captcha.
- Tesla rate-limits aggressive scraping — the client retries with backoff on
  429/5xx. Requests run sequentially through the browser context.
- The profile dir is exclusive — close any other instance of the same browser
  channel before running, or use `--executable-path` / `--channel` to pick a
  different one. (Brave with its own profile dir runs alongside your normal
  Brave fine.)
