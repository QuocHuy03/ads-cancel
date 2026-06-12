# ads-cancel

Bulk re-appeal tool for Google Ads MCC sub-accounts suspended for
"Circumventing systems: Multiple account abuse".

## What it does

1. Paste your Google Ads session cookie.
2. Auto-discovers your MCC + session IDs.
3. Lists every sub-account, classifying each one as:
   - **Multi-account abuse** (the target — re-appeal works)
   - **Suspicious payment** (different tag set)
   - **Billing / unpaid**
   - **Manager**
4. Submits the suspension re-appeal for every checked row.
5. Shows OK / PENDING / BLACKLISTED per row, accumulating history across runs.

## Run from source

```bash
pip install PyQt5 requests
python ui.py
```

CLI without UI:

```bash
python auto.py --dry-run --limit 5
python auto.py --yes
```

## macOS pre-built .app

The GitHub Actions workflow builds Intel + Apple Silicon `.app` bundles on
every push. Grab the latest from the **Actions** tab → pick a run → download
the `AdsCancel-macos-arm64` (or `x86_64`) artifact.

When frozen, the app reads/writes:

```
~/.adscancel/cookie.txt
~/.adscancel/appeal_results.json
```

## Getting the cookie

1. Log in to `https://ads.google.com` in Chrome.
2. F12 → Network tab → reload the page.
3. Click any request to `ads.google.com` → Headers → copy the entire
   `Cookie:` header value.
4. Paste into the textarea and click **Save cookie**.

The rotating bits (`SIDCC`, `__Secure-*PSIDCC`, `__Secure-*PSIDTS`) expire
every ~10–20 minutes; re-paste a fresh cookie when scans start failing.
The UI merges newly pasted cookies with what's already on disk, so you can
paste a partial header and the long-lived `SID`/`HSID`/`APISID` are kept.

## Security

`cookie.txt` is your live Google session — `.gitignore` excludes it. Never
commit it.
