import json, os, re, smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
import requests, yfinance as yf
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
CONFIG=json.loads((ROOT/"config.json").read_text())
DATA_FILE=ROOT/"data.json"
UA={"User-Agent":"Mozilla/5.0 (compatible; FMG-DCF-Monitor/2.0)"}

def yahoo_market_inputs():
    info=yf.Ticker(CONFIG["ticker"]).info
    price=info.get("currentPrice") or info.get("regularMarketPrice")
    growth=info.get(CONFIG["growth_field"])
    if price is None or growth is None: raise RuntimeError("Yahoo price/growth unavailable.")
    fxinfo=yf.Ticker("AUDUSD=X").info
    audusd=fxinfo.get("regularMarketPrice") or fxinfo.get("currentPrice")
    if not audusd: raise RuntimeError("AUD/USD unavailable.")
    return float(price),float(growth),1/float(audusd)

def annual_fcf_and_shares():
    t=yf.Ticker(CONFIG["ticker"])
    cf=t.cashflow
    bs=t.balance_sheet
    if cf is None or cf.empty or bs is None or bs.empty: raise RuntimeError("Annual statements unavailable.")
    if "Free Cash Flow" not in cf.index: raise RuntimeError("Free Cash Flow row unavailable.")
    fcf=cf.loc["Free Cash Flow"].dropna()
    share_row=None
    for candidate in ["Ordinary Shares Number","Share Issued"]:
        if candidate in bs.index:
            share_row=bs.loc[candidate].dropna(); break
    if share_row is None: raise RuntimeError("Annual shares outstanding row unavailable.")
    rows=[]
    for c in sorted([c for c in fcf.index if c in share_row.index], reverse=True):
        f=float(fcf[c]); s=float(share_row[c])
        if s>0: rows.append({"year":str(getattr(c,"year",c)),"fcf_usd":f,"shares":s,"fcf_per_share_usd":f/s})
    if len(rows)<CONFIG["fcf_years"]: raise RuntimeError(f"Only {len(rows)} annual FCF/share observations found; need {CONFIG['fcf_years']}.")
    rows=rows[:CONFIG["fcf_years"]]
    return sum(x["fcf_per_share_usd"] for x in rows)/len(rows), rows

def gurufocus_pe():
    r=requests.get("https://www.gurufocus.com/term/pe-ratio/ASX%3AFMG",headers=UA,timeout=30); r.raise_for_status()
    text=BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)
    m=re.search(r"10-year median(?:\s+of)?\s*([0-9]+(?:\.[0-9]+)?)",text,re.I)
    if not m: raise RuntimeError("10Y median P/E not found.")
    return float(m.group(1))

def intrinsic(fcf_usd,fx,growth,pe):
    start=fcf_usd*fx; rr=CONFIG["required_return"]; n=CONFIG["forecast_years"]; pv=0
    for y in range(1,n+1):
        projected=start*((1+growth)**y)
        pv += projected/((1+rr)**y)
    y10=start*((1+growth)**n)
    pv += (y10*pe)/((1+rr)**n)
    return start,pv

def send_email(d):
    user=os.getenv("SMTP_USERNAME","").strip(); pw=os.getenv("SMTP_APP_PASSWORD","").strip()
    if not user or not pw: raise RuntimeError("SMTP secrets missing.")
    msg=EmailMessage(); msg["From"]=user; msg["To"]=CONFIG["alert_email"]
    msg["Subject"]=f"FMG undervalued alert — A${d['price_aud']:.2f} vs A${d['intrinsic_value_aud']:.2f}"
    msg.set_content(f"""FMG has crossed below calculated intrinsic value.

Price: A${d['price_aud']:.2f}
Intrinsic value: A${d['intrinsic_value_aud']:.2f}
7-year average FCF/share: A${d['avg_fcf_per_share_aud']:.2f}
Yahoo growth: {d['growth_rate']*100:.2f}%
Required return: 15%
GuruFocus 10Y median P/E: {d['median_pe']:.2f}x
""")
    with smtplib.SMTP("smtp.gmail.com",587,timeout=30) as s:
        s.starttls(); s.login(user,pw); s.send_message(msg)

def main():
    old=json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}
    price,growth,fx=yahoo_market_inputs()
    avg_usd,hist=annual_fcf_and_shares()
    try:
        pe=gurufocus_pe(); gf=f"Live GuruFocus: {pe:.2f}x"
    except Exception as e:
        pe=float(CONFIG["fallback_median_pe"]); gf=f"Fallback {pe:.2f}x ({e})"
    avg_aud,value=intrinsic(avg_usd,fx,growth,pe)
    under=price<value
    discount=(value-price)/value*100
    d={"price_aud":price,"intrinsic_value_aud":value,"discount_to_value_pct":discount,
       "avg_fcf_per_share_usd":avg_usd,"avg_fcf_per_share_aud":avg_aud,
       "growth_rate":growth,"median_pe":pe,"usd_to_aud":fx,"undervalued":under,
       "updated_at":datetime.now(timezone.utc).isoformat(),"fcf_history":hist,
       "sources":{"fundamentals":"Annual total FCF divided by corresponding annual shares outstanding",
                  "yahoo":"Live Yahoo Finance price, growth and FX","gurufocus":gf}}
    DATA_FILE.write_text(json.dumps(d,indent=2))
    if under and not bool(old.get("undervalued",False)): send_email(d)

if __name__=="__main__": main()
