import os
import json
import time
import random
from datetime import datetime, timezone

import requests
import feedparser
from openai import OpenAI


# -------- Helpers --------

def read_holdings():
    with open("holdings.json", "r", encoding="utf-8") as f:
        return json.load(f)


def get_with_retry(url, max_tries=6):
    """
    Retry on 429 rate limit (common on GitHub Actions shared IPs).
    Uses exponential backoff + jitter.
    """
    last_exc = None
    for attempt in range(max_tries):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 429:
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
    Fetch ALL symbols in ONE Yahoo request to reduce rate limiting.
    Returns dict: { "AAPL": {price, change_pct, ...}, ... }
    """
    # Remove duplicates while preserving order
    seen = set()
    symbols = [s for s in symbols if s and not (s in seen or seen.add(s))]

    joined = ",".join(symbols)
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={joined}"

    r = get_with_retry(url)
    data = r.json().get("quoteResponse", {}).get("result", [])

    out = {}
    for q in data:
        sym = q.get("symbol")
        out[sym] = {
            "symbol": sym,
            "name": q.get("shortName") or q.get("longName") or sym,
            "price": q.get("regularMarketPrice"),
            "change": q.get("regularMarketChange"),
            "change_pct": q.get("regularMarketChangePercent"),
            "currency": q.get("currency"),
        }
    return out


def news_for(ticker, limit=3):
    """
    Google News RSS headlines (no API key).
    """
    q = requests.utils.quote(f"{ticker} stock")
    rss = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
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
    if not q:
        return f"- {sym}: (no data)"
    price = q.get("price")
    cpct = q.get("change_pct")
    cur = q.get("currency") or ""
    if price is None or cpct is None:
        return f"- {sym}: (no data)"
    return f"- {sym}: {price} {cur} ({cpct:+.2f}%)"


def ai_summarize(raw_text):
    """
    Produce a short weekly brief using OpenAI if OPENAI_API_KEY exists.
    Falls back to raw_text if key is missing or API fails.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None  # signal: no AI

    try:
        client = OpenAI(api_key=api_key)

        instructions = (
            "You are a weekly market brief assistant.\n"
            "Summarize the user's portfolio + market pulse based ONLY on the provided text.\n"
            "Rules:\n"
            "- Keep it under ~1200 characters.\n"
            "- Use 6–10 bullets.\n"
            "- Structure:\n"
            "  1) Market pulse (1–2 bullets)\n"
            "  2) Biggest movers in my tickers (2–4 bullets)\n"
            "  3) What to watch next week (2–3 bullets)\n"
            "- No financial advice. No buy/sell language.\n"
            "- Plain English.\n"
        )

        resp = client.responses.create(
            model="gpt-5.2",
            input=raw_text,
            instructions=instructions
        )
        text = (resp.output_text or "").strip()
        return text if text else None
    except Exception:
        return None


# -------- Main --------

def main():
    data = read_holdings()
    tickers = data.get("tickers", [])
    indices = data.get("indices", [])
    all_symbols = tickers + indices

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    lines.append("📊 Weekly Market & Economy Update")
    lines.append(f"🕒 Generated: {now_utc}")
    lines.append("")

    # Quotes (safe fallback)
    quotes = {}
    yahoo_failed = False
    try:
        quotes = yf_quotes(all_symbols)
    except Exception:
        yahoo_failed = True
        quotes = {}

    # Your stocks
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

    # Headlines
    lines.append("")
    lines.append("📰 Headlines")
    if not tickers:
        lines.append("- (no tickers to fetch news for)")
    else:
        for t in tickers:
            try:
                hs = news_for(t, limit=3)
                if not hs:
                    lines.append(f"- {t}: (no headlines found)")
                else:
                    for h in hs:
                        lines.append(f"- {t}: {h}")
            except Exception:
                lines.append(f"- {t}: (news fetch failed)")

    if yahoo_failed:
        lines.append("")
        lines.append("⚠️ Note: price data fetch failed (rate-limit/network). Headlines still included.")

    raw_msg = "\n".join(lines)
    raw_msg = raw_msg[:3800]  # keep bounded for Telegram + OpenAI input

    # AI Summary (optional)
    summary = ai_summarize(raw_msg)

    if summary:
        # One message: summary first, then a shorter details block
        details = raw_msg
        # Keep details shorter so total stays within Telegram limits comfortably
        details = details[:2000]

        final_msg = (
            "🧠 AI Weekly Brief\n"
            f"{summary}\n\n"
            "—\n"
            "📌 Details\n"
            f"{details}"
        )
    else:
        # No AI key or AI failed -> send raw
        final_msg = raw_msg + "\n\nTip: Add OPENAI_API_KEY secret to enable AI summary."

    final_msg = final_msg[:3800]
    send_telegram(final_msg)


if __name__ == "__main__":
    main()
