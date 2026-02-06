import os
import json
from datetime import datetime, timezone
import requests
import feedparser

USE_OPENAI = bool(os.getenv("OPENAI_API_KEY"))

def read_holdings():
    with open("holdings.json", "r", encoding="utf-8") as f:
        return json.load(f)

def yf_quote(symbol):
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()["quoteResponse"]["result"]
    if not data:
        return None
    q = data[0]
    return {
        "symbol": q.get("symbol"),
        "price": q.get("regularMarketPrice"),
        "change": q.get("regularMarketChange"),
        "change_pct": q.get("regularMarketChangePercent"),
    }

def news_for(ticker):
    rss = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(rss)
    return [e.title for e in feed.entries[:3]]

def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }
    requests.post(url, json=payload, timeout=15)

def main():
    data = read_holdings()
    tickers = data["tickers"]
    indices = data["indices"]

    lines = []
    lines.append("📊 Weekly Market & Economy Update")
    lines.append(datetime.now(timezone.utc).strftime("Generated %Y-%m-%d %H:%M UTC"))
    lines.append("")

    lines.append("💼 Your Stocks")
    for t in tickers:
        q = yf_quote(t)
        if q:
            lines.append(f"- {t}: {q['price']} ({q['change_pct']:+.2f}%)")

    lines.append("")
    lines.append("🌍 Market Pulse")
    for i in indices:
        q = yf_quote(i)
        if q:
            lines.append(f"- {i}: {q['price']} ({q['change_pct']:+.2f}%)")

    lines.append("")
    lines.append("📰 Headlines")
    for t in tickers:
        headlines = news_for(t)
        for h in headlines:
            lines.append(f"- {t}: {h}")

    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
