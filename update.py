import json, os, re, smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from io import StringIO

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT/"config.json").read_text())
DATA_FILE = ROOT/"data.json"
SEED_FILE = ROOT/"history_seed.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
}

CF_URL = "https://stockanalysis.com/quote/asx/FMG/financials/cash-flow-statement/"
BS_URL = "https://stockanalysis.com/quote/asx/FMG/financials/balance-sheet/"
GF_URL = "https://www.gurufocus.com/term/pe-ratio/ASX%3AFMG"

def fetch_tables(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return pd.read_html(StringIO(r.text))

def normalise_year(col):
    s = str(col)
    m = re.search(r"FY\s*(20\d{2})", s, re.I)
    return m.group(1) if m else None

def extract_row_from_tables(tables, row_names):
    for df in tables:
        if df.empty:
            continue
        first = df.columns[0]
        labels = df[first].astype(str).str.strip()
        for target in row_names:
            matches = df[labels.str.lower() == target.lower()]
            if not matches.empty:
                row = matches.iloc[0]
                out = {}
                for col in df.columns[1:]:
                    year = normalise_year(col)
                    if not year:
                        continue
                    val = row[col]
                    if pd.isna(val) or str(val).strip() in {"-", "—", ""}:
                        continue
                    text = str(val).replace(",", "").strip()
                    try:
                        out[year] = float(text)
                    except ValueError:
                        pass
                if out:
                    return out
    return {}

def refresh_fundamental_history():
    history = json.loads(SEED_FILE.read_text())

    cf_tables = fetch_tables(CF_URL)
    bs_tables = fetch_tables(BS_URL)

    fcf = extract_row_from_tables(cf_tables, ["Free Cash Flow"])
    shares = extract_row_from_tables(bs_tables, [
        "Total Common Shares Outstanding",
        "Filing Date Shares Outstanding"
    ])

    # StockAnalysis reports these statement tables in millions.
    refreshed = 0
    for year in sorted(set(fcf) & set(shares)):
        if fcf[year] > 0 and shares[year] > 0:
            history[year] = {
                "fcf_usd_m": float(fcf[year]),
                "shares_m": float(shares[year])
            }
            refreshed += 1

    if refreshed == 0:
        raise RuntimeError("Could not refresh any annual FCF/share-count pairs from StockAnalysis.")

    latest_years = sorted(history.keys(), key=int, reverse=True)[:CONFIG["fcf_years"]]
    latest_years = sorted(latest_years, key=int)

    if len(latest_years) < CONFIG["fcf_years"]:
        raise RuntimeError(f"Only {len(latest_years)} annual periods available; need {CONFIG['fcf_years']}.")

    rows = []
    for year in latest_years:
        f = float(history[year]["fcf_usd_m"])
        s = float(history[year]["shares_m"])
        rows.append({
            "year": year,
            "fcf_usd_m": f,
            "shares_m": s,
            "fcf_per_share_usd": f / s
        })

    avg = sum(x["fcf_per_share_usd"] for x in rows) / len(rows)
    return avg, rows, refreshed

def yahoo_market_inputs():
    info = yf.Ticker(CONFIG["ticker"]).info
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    growth = info.get(CONFIG["growth_field"])
    if price is None:
        raise RuntimeError("Yahoo Finance did not return FMG current price.")
    if growth is None:
        raise RuntimeError("Yahoo Finance did not return earningsQuarterlyGrowth.")

    fx = yf.Ticker("AUDUSD=X").info
    audusd = fx.get("regularMarketPrice") or fx.get("currentPrice")
    if audusd is None or float(audusd) <= 0:
        raise RuntimeError("Yahoo Finance did not return AUD/USD.")
    return float(price), float(growth), 1.0 / float(audusd)

def gurufocus_median_pe():
    r = requests.get(GF_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    m = re.search(r"10-year median(?:\s+of)?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if not m:
        raise RuntimeError("GuruFocus 10-year median P/E not found.")
    return float(m.group(1))

def calculate_intrinsic_value(avg_fcf_usd, usd_to_aud, growth, pe):
    starting_fcf_aud = avg_fcf_usd * usd_to_aud
    r = CONFIG["required_return"]
    n = CONFIG["forecast_years"]

    pv_fcfs = 0.0
    for year in range(1, n + 1):
        projected = starting_fcf_aud * ((1 + growth) ** year)
        pv_fcfs += projected / ((1 + r) ** year)

    year10_fcf = starting_fcf_aud * ((1 + growth) ** n)
    terminal_value = year10_fcf * pe
    pv_terminal = terminal_value / ((1 + r) ** n)

    return starting_fcf_aud, pv_fcfs + pv_terminal

def send_email(d):
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_APP_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError("SMTP_USERNAME or SMTP_APP_PASSWORD is missing.")

    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = CONFIG["alert_email"]
    msg["Subject"] = (
        f"FMG undervalued alert — A${d['price_aud']:.2f} "
        f"vs A${d['intrinsic_value_aud']:.2f}"
    )
    msg.set_content(f"""FMG (ASX: FMG) has crossed below your calculated intrinsic value.

Current price: A${d['price_aud']:.2f}
Intrinsic value: A${d['intrinsic_value_aud']:.2f}
Discount to intrinsic value: {d['discount_to_value_pct']:.1f}%

7-year average FCF/share: A${d['avg_fcf_per_share_aud']:.2f}
Yahoo earnings growth (YoY): {d['growth_rate']*100:.2f}%
Required return: {d['required_return']*100:.2f}%
GuruFocus 10-year median P/E: {d['median_pe']:.2f}x

Updated: {d['updated_at']}

Mechanical valuation monitor only; not financial advice.
""")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)

def main():
    old = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}

    avg_fcf_usd, history, refreshed = refresh_fundamental_history()
    price, growth, usd_to_aud = yahoo_market_inputs()

    try:
        pe = gurufocus_median_pe()
        gf_status = f"Live GuruFocus: {pe:.2f}x"
    except Exception as e:
        pe = float(CONFIG["fallback_median_pe"])
        gf_status = f"Fallback {pe:.2f}x; live fetch failed: {e}"

    avg_fcf_aud, value = calculate_intrinsic_value(
        avg_fcf_usd, usd_to_aud, growth, pe
    )

    undervalued = price < value
    discount_pct = ((value - price) / value) * 100.0

    d = {
        "price_aud": price,
        "intrinsic_value_aud": value,
        "discount_to_value_pct": discount_pct,
        "avg_fcf_per_share_usd": avg_fcf_usd,
        "avg_fcf_per_share_aud": avg_fcf_aud,
        "growth_rate": growth,
        "required_return": CONFIG["required_return"],
        "forecast_years": CONFIG["forecast_years"],
        "median_pe": pe,
        "usd_to_aud": usd_to_aud,
        "undervalued": undervalued,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fcf_history": history,
        "sources": {
            "fundamentals": (
                f"7-year rolling history; {refreshed} recent annual periods "
                "refreshed from StockAnalysis/S&P Global tables"
            ),
            "yahoo": "Live Yahoo Finance price, earnings growth and AUD/USD",
            "gurufocus": gf_status
        }
    }

    DATA_FILE.write_text(json.dumps(d, indent=2))

    was_undervalued = bool(old.get("undervalued", False))
    if undervalued and not was_undervalued:
        send_email(d)

if __name__ == "__main__":
    main()
