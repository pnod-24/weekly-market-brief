import os
import json
import time
import random
from datetime import datetime, timezone

import requests
import feedparser

USE_OPENAI = bool(os.getenv("OPENAI_API_KEY"))  # not used in this simple version, but kept


def read_holdings():
    with open("holdings.json", "r", encoding="utf-8") as f:
        return json.load(f)


def get_with_retry(url, max_tries=6):
    """
    Retry on Yahoo 429 rate limit with exponential backoff.
    This is important on GitHub Actions because runner IPs are shared.
    """
    last_exc = None
    for attempt in range(max_tries):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 429:
                # Exponential backoff + jitter
                sleep_s = (2 ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            sleep_s = (2 ** attempt) + random.uniform(0.5, 1.5)
            time.sleep(sleep_s)
    raise last_exc


def yf_quotes(symbols):
    """
    Fetch ALL symbols in ONE request to reduce rate limiting.
    Returns a dict: { "AAPL": {price, change_pct...}, ... }
    """
    # Remove duplicates while preserving order
    seen = set()
    symbols = [s for s in symbols if not (s in seen or seen.add(s))]

    joined = ",".join(symbols)
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={joined}"

    r = get_with_retry(url)
    data = r.json().get("quoteResponse", {}).get("result", [])

    out = {}
    for q in data:
        sym = q.get("symbol")
        out[sym] = {
            "symbol": sym,
            "price": q.get("regularMarketPrice"),
            "change": q.get("regularMarketChange"),
            "change_pct": q.get("regularMarketChangePercent"),
            "currency": q.get("currency"),
        }
    return out


def news_for(ticker, limit=3):
    """
    Google News RSS headlines (no API key needed).
    """
    rss = f"https://news.google.com/rss/search?q={requests.utils.quote(ticker + ' stock')}&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(rss)
    titles = []
    for e in feed.entries[:limit]:
        t = (e.get("title") or "").strip()
        if t:
            titles.append(t)
    return titles


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in GitHub Secrets.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def fmt_line(sym, q):
    """
    Format a single quote line safely.
    """
    if not q:
        return f"- {sym}: (no data)"

    price = q.get("price")
    cpct = q.get("change_pct")
    cur = q.get("currency") or ""

    if price is None or cpct is None:
        return f"- {sym}: (no data)"

    # Keep it simple; Yahoo already returns numeric
    return f"- {sym}: {price} {cur} ({cpct:+.2f}%)"


def main():
    data = read_holdings()
    tickers = data.get("tickers", [])
    indices = data.get("indices", [])

    # Build one combined list for ONE Yahoo call
    all_symbols = tickers + indices

    lines = []
    lines.append("📊 Weekly Market & Economy Update")
    lines.append(datetime.now(timezone.utc).strftime("Generated %Y-%m-%d %H:%M UTC"))
    lines.append("")

    # 1) Quotes (safe: do not crash everything if Yahoo fails)
    quotes = {}
    yahoo_error = None
    try:
        quotes = yf_quotes(all_symbols)
    except Exception as e:
        yahoo_error = str(e)

    # 2) Your stocks section
    lines.append("💼 Your Stocks")
    if not tickers:
        lines.append("- (no tickers in holdings.json)")
    else:
        for t in tickers:
            lines.append(fmt_line(t, quotes.get(t)))

    lines.append("")
    lines.append("🌍 Market Pulse")
    if not indices:
        lines.append("- (no indices in holdings.json)")
    else:
        for i in indices:
            lines.append(fmt_line(i, quotes.get(i)))

    # 3) Headlines (safe: don’t crash on RSS errors)
    lines.append("")
    lines.append("📰 Headlines")
    if not tickers:
        lines.append("- (no tickers to fetch news for)")
    else:
        for t in tickers:
            try:
                headlines = news_for(t, limit=3)
                if not headlines:
                    lines.append(f"- {t}: (no headlines found)")
                else:
                    for h in headlines:
                        lines.append(f"- {t}: {h}")
            except Exception:
                lines.append(f"- {t}: (news fetch failed)")

    # 4) If Yahoo failed, add a short note (don’t crash)
    if yahoo_error:
        lines.append("")
        lines.append("⚠️ Note: Price data fetch hit an error (rate-limit or network).")
        lines.append("    Headlines still sent. Next run should recover automatically.")

    # Telegram message limit exists; keep safe
    msg = "\n".join(lines)
    msg = msg[:3800]

    send_telegram(msg)


if __name__ == "__main__":
    main()
