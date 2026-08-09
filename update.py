import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
import yfinance as yf
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
DATA_FILE = ROOT / "data.json"

UA = {"User-Agent": "Mozilla/5.0 (compatible; FMG-DCF-Monitor/1.0)"}

# Seed values are intentionally NOT used for live alerts.
# They are here only to document the historical series used when the project was built.
DOCUMENTED_FCF_USD = [1.08, 1.45, 2.99, 1.25, 1.47, 1.65, 1.04]  # FY2019–FY2025

def yahoo_market_inputs():
    fmg = yf.Ticker(CONFIG["ticker"])
    info = fmg.info

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    growth = info.get(CONFIG["growth_field"])

    if price is None:
        raise RuntimeError("Yahoo Finance did not return FMG current price.")
    if growth is None:
        raise RuntimeError("Yahoo Finance did not return earningsQuarterlyGrowth.")

    audusd = yf.Ticker("AUDUSD=X").info
    rate = audusd.get("regularMarketPrice") or audusd.get("currentPrice")
    if rate is None or float(rate) <= 0:
        raise RuntimeError("Yahoo Finance did not return AUD/USD.")

    # AUDUSD=X = USD for A$1, therefore invert to obtain A$ per US$1.
    usd_to_aud = 1.0 / float(rate)
    return float(price), float(growth), usd_to_aud

def gurufocus_10y_median_pe():
    url = "https://www.gurufocus.com/term/pe-ratio/ASX%3AFMG"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    m = re.search(r"10-year median(?:\s+of)?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if not m:
        raise RuntimeError("GuruFocus page loaded but 10-year median P/E was not found.")
    return float(m.group(1))

def msn_7yr_average_fcf_per_share():
    """
    MSN Money's page URLs and rendered markup can change. For safety this updater
    requires a current MSN FMG page URL to be stored in the GitHub secret
    MSN_FMG_FCF_URL. If parsing fails, the run stops and NO valuation alert is sent.

    This avoids silently emailing from stale FCF data.
    """
    url = os.getenv("MSN_FMG_FCF_URL", "").strip()
    if not url:
        raise RuntimeError(
            "MSN_FMG_FCF_URL secret is missing. Add the exact MSN Money FMG financial-analysis URL."
        )

    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    # First attempt: JSON/script text around Free Cash Flow Per Share.
    anchor = re.search(r"free\s*cash\s*flow\s*per\s*share", text, re.I)
    if not anchor:
        # MSN often hydrates data in script tags.
        scripts = " ".join(s.get_text(" ", strip=True) for s in soup.find_all("script"))
        anchor = re.search(r"free\s*cash\s*flow\s*per\s*share", scripts, re.I)
        text = scripts

    if not anchor:
        raise RuntimeError("MSN page did not expose 'Free Cash Flow Per Share' in parsable HTML.")

    window = text[anchor.start(): anchor.start()+12000]

    # Pull decimal values; filter to a plausible per-share range.
    # We require at least 7 values and use the first 7 after the metric label.
    raw = re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])", window)
    vals = []
    for x in raw:
        v = float(x)
        if -20 <= v <= 20:
            vals.append(v)

    if len(vals) < 7:
        raise RuntimeError(f"MSN parser found only {len(vals)} plausible FCF/share values; need 7.")

    seven = vals[:7]
    # Refuse suspicious series dominated by years/percent artifacts.
    if all(abs(v) < 0.01 for v in seven):
        raise RuntimeError("MSN parser produced an implausible FCF/share series.")

    return sum(seven) / 7.0, seven

def intrinsic_value(fcf_usd, usd_to_aud, growth, median_pe):
    start = fcf_usd * usd_to_aud
    rr = CONFIG["required_return"]
    years = CONFIG["forecast_years"]

    pv_fcfs = 0.0
    for year in range(1, years + 1):
        fcf = start * ((1.0 + growth) ** year)
        pv_fcfs += fcf / ((1.0 + rr) ** year)

    year10_fcf = start * ((1.0 + growth) ** years)
    terminal = year10_fcf * median_pe
    pv_terminal = terminal / ((1.0 + rr) ** years)

    return start, pv_fcfs + pv_terminal

def send_email(data):
    username = os.getenv("SMTP_USERNAME", "").strip()
    app_password = os.getenv("SMTP_APP_PASSWORD", "").strip()

    if not username or not app_password:
        raise RuntimeError("SMTP_USERNAME / SMTP_APP_PASSWORD secrets are not configured.")

    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = CONFIG["alert_email"]
    msg["Subject"] = f"FMG undervalued alert — A${data['price_aud']:.2f} vs A${data['intrinsic_value_aud']:.2f}"

    msg.set_content(f"""FMG (ASX: FMG) has crossed below your calculated intrinsic value.

Current price: A${data['price_aud']:.2f}
Intrinsic value: A${data['intrinsic_value_aud']:.2f}
Discount to intrinsic value: {data['discount_to_value_pct']:.1f}%

Inputs:
7-year average FCF/share: A${data['avg_fcf_per_share_aud']:.2f}
Yahoo earnings growth (YoY): {data['growth_rate']*100:.2f}%
Required return: {data['required_return']*100:.2f}%
GuruFocus 10-year median P/E: {data['median_pe']:.2f}x

Updated: {data['updated_at']}

Mechanical valuation alert only; not financial advice.
""")

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, app_password)
        smtp.send_message(msg)

def main():
    previous = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}

    price, growth, usd_to_aud = yahoo_market_inputs()

    fcf_usd, fcf_series = msn_7yr_average_fcf_per_share()

    try:
        median_pe = gurufocus_10y_median_pe()
        gf_status = f"Live GuruFocus: {median_pe:.2f}x"
    except Exception as e:
        # We allow the verified 7.86x fallback, but mark it explicitly.
        median_pe = float(CONFIG["fallback_median_pe"])
        gf_status = f"Fallback {median_pe:.2f}x (live fetch failed: {e})"

    fcf_aud, value = intrinsic_value(fcf_usd, usd_to_aud, growth, median_pe)
    undervalued = price < value
    discount_pct = (value - price) / value * 100.0

    data = {
        "ticker": CONFIG["ticker"],
        "price_aud": price,
        "intrinsic_value_aud": value,
        "discount_to_value_pct": discount_pct,
        "avg_fcf_per_share_usd": fcf_usd,
        "avg_fcf_per_share_aud": fcf_aud,
        "fcf_series_usd": fcf_series,
        "growth_rate": growth,
        "required_return": CONFIG["required_return"],
        "forecast_years": CONFIG["forecast_years"],
        "median_pe": median_pe,
        "usd_to_aud": usd_to_aud,
        "undervalued": undervalued,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "msn": "Live MSN Money: 7 annual FCF/share values parsed",
            "yahoo": "Live Yahoo Finance: price, earnings growth and AUD/USD",
            "gurufocus": gf_status
        }
    }

    DATA_FILE.write_text(json.dumps(data, indent=2))

    was_under = bool(previous.get("undervalued", False))
    if undervalued and not was_under:
        send_email(data)

if __name__ == "__main__":
    main()
