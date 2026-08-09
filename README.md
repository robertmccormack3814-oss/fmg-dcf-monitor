# FMG DCF Monitor

This project is designed to run on the same GitHub Pages setup as your alcohol-free tracker, with GitHub Actions doing the scheduled data refresh.

## Inputs

- **FMG market price:** Yahoo Finance
- **Growth:** Yahoo Finance `earningsQuarterlyGrowth` (quarterly earnings growth YoY)
- **7-year FCF/share:** MSN Money
- **Currency conversion:** Yahoo Finance AUD/USD, inverted so all valuation figures are AUD
- **Required return:** 15%
- **Terminal multiple:** GuruFocus 10-year median P/E (live attempt, verified fallback 7.86x)
- **Forecast:** 10 years

## Alert

Recipient is already configured as:

`robertmccormack3814@gmail.com`

An email is sent only when the stock crosses from **price >= intrinsic value** to **price < intrinsic value**.

## Important safety choice

The program **will not use stale fallback FCF data for an alert**. If MSN changes its page and the scraper cannot read seven annual FCF/share values, that GitHub Action run fails and no valuation email is sent.

That is intentional: a broken scrape should not look like a valid investment signal.

## Setup

1. Upload every file/folder in this project to your GitHub repository.
2. Keep `index.html`, `update.py`, `config.json`, `requirements.txt`, and `data.json` at repository root.
3. Keep `.github/workflows/update.yml` in exactly that folder structure.
4. GitHub repository → **Settings → Secrets and variables → Actions → New repository secret**.
5. Add:
   - `SMTP_USERNAME` = your Gmail address
   - `SMTP_APP_PASSWORD` = a Google **App Password**, not your normal Gmail password
   - `MSN_FMG_FCF_URL` = the exact MSN Money page URL where you can see FMG's Free Cash Flow Per Share history
6. Go to **Actions → Update FMG DCF → Run workflow** for the first test.
7. GitHub Pages can remain **Deploy from branch → main → / (root)**.

## Gmail

Google normally requires 2-Step Verification before you can create an App Password. Never paste your App Password into the source code or commit it to GitHub. Store it only as the GitHub secret `SMTP_APP_PASSWORD`.

## Model formula

For each forecast year:

`FCF_t = average_7Y_FCF_per_share_AUD × (1 + Yahoo_growth)^t`

`PV_t = FCF_t / (1 + 15%)^t`

Terminal value:

`TV_10 = FCF_10 × GuruFocus_10Y_median_PE`

Intrinsic value is the sum of the discounted ten annual FCF values plus discounted terminal value.

## Caveat on growth

Yahoo's `earningsQuarterlyGrowth` is a short-term YoY earnings-growth input. Applying it unchanged for ten years can produce an extreme valuation when growth is unusually high, low, or negative. The dashboard shows the live growth rate so you can see exactly what the model is using.

Mechanical valuation monitor only; not financial advice.
