#!/usr/bin/env python3
import os
import sqlite3
import requests
import feedparser
from datetime import datetime, timedelta
from dateutil import parser as dateparser
from bs4 import BeautifulSoup
import time
from langdetect import detect, LangDetectException

USE_TELEGRAM = os.environ.get("USE_TELEGRAM", "true").lower() == "true"
DB_PATH = "seen.db"
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "7"))
KEYWORDS = [
    "llm", "large language model", "gpt", "openai", "chatgpt", "multimodal",
    "agents", "retrieval-augmented", "rag", "embeddings", "fine-tune", "finetune",
    "prompt engineering", "diffusion", "stable diffusion", "text-to-image",
    "commercial", "startup", "use case", "plugin", "tooling", "neural", "ai", "искусственный интеллект", "нейросеть", "нейросети"
]

# RSS feeds: русские + англоязычные
RSS_FEEDS = [
    # Russian
    "https://habr.com/ru/rss/all/all/?fl=ru",
    "https://vc.ru/rss",
    "https://tjournal.ru/rss",
    # English / Global
    "https://huggingface.co/blog/rss.xml",
    "https://openai.com/blog/rss/",
    "https://arxiv.org/rss/cs.AI",
    "https://www.theverge.com/rss/index.xml",
    "https://www.reddit.com/r/MachineLearning/.rss",
    "https://www.reddit.com/r/ArtificialInteligence/.rss"
]

NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY")

# Translation settings
TRANSLATE = os.environ.get("TRANSLATE", "true").lower() == "true"
TRANSLATE_PROVIDER = os.environ.get("TRANSLATE_PROVIDER", "libre").lower()  # 'libre' or 'deepl'
TRANSLATE_API_URL = os.environ.get("TRANSLATE_API_URL", "https://libretranslate.de/translate")
TRANSLATE_API_KEY = os.environ.get("TRANSLATE_API_KEY", "")  # optional for provider
DEEPL_API_URL = "https://api-free.deepl.com/v2/translate"
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "")

# DB helpers
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, when_sent DATETIME)")
    conn.commit()
    return conn

def is_seen(conn, url):
    c = conn.cursor()
    c.execute("SELECT 1 FROM seen WHERE url = ?", (url,))
    return c.fetchone() is not None

def mark_seen(conn, url):
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO seen (url, when_sent) VALUES (?, ?)", (url, datetime.utcnow()))
    conn.commit()

# Fetchers
def fetch_rss():
    items = []
    for feed in RSS_FEEDS:
        try:
            d = feedparser.parse(feed)
            for e in d.entries:
                title = e.get("title", "")
                link = e.get("link", "")
                published = None
                if "published" in e:
                    try:
                        published = dateparser.parse(e.published)
                    except:
                        published = None
                summary = e.get("summary", "") or e.get("description", "")
                items.append({"title": title, "link": link, "published": published, "summary": summary, "source": feed})
        except Exception as ex:
            print("RSS error", feed, ex)
    return items

def fetch_newsapi():
    items = []
      if not NEWSAPI_KEY:
        return items
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": " OR ".join(KEYWORDS[:8]),
        "language": "en",
        "pageSize": 20,
        "apiKey": NEWSAPI_KEY,
        "sortBy": "publishedAt"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        j = r.json()
        for art in j.get("articles", []):
            items.append({
                "title": art.get("title"),
                "link": art.get("url"),
                "published": dateparser.parse(art.get("publishedAt")) if art.get("publishedAt") else None,
                "summary": art.get("description") or art.get("content") or ""
            })
    except Exception as ex:
        print("NewsAPI error", ex)
    return items

# Scoring + filtering
def score_item(item):
    text = (item.get("title","") + " " + item.get("summary","")).lower()
    score = 0
    for k in KEYWORDS:
        if k in text:
            score += 1
    if item.get("published"):
        try:
            delta = datetime.utcnow() - item["published"].replace(tzinfo=None)
            hours = delta.total_seconds()/3600
            if hours < 48:
                score += 2
            elif hours < 168:
                score += 1
        except:
            pass
    return score

# Extract summary if missing
def extract_first_sentence(url):
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent":"news-digest-bot/1.0"})
        soup = BeautifulSoup(r.text, "lxml")
        desc = soup.find("meta", {"name":"description"}) or soup.find("meta", {"property":"og:description"})
        if desc and desc.get("content"):
            return desc["content"].strip()
        p = soup.find("p")
        if p:
            txt = p.get_text().strip()
            if len(txt) > 20:
                return txt
    except Exception:
        return ""
    return ""

# Simple language detection: prefer langdetect
def looks_russian(text):
    if not text or len(text.strip()) < 10:
        return False
    try:
        lang = detect(text)
        return lang == "ru"
    except LangDetectException:
        # fallback: check for Cyrillic characters
        return any('а' <= ch <= 'я' or 'А' <= ch <= 'Я' for ch in text)

# Translation functions
def translate_libre(text, source="auto", target="ru"):
    try:
        payload = {"q": text, "source": source, "target": target, "format":"text"}
        headers = {"Content-Type":"application/json"}
        if TRANSLATE_API_KEY:
            # some instances accept api_key in payload
            payload["api_key"] = TRANSLATE_API_KEY
        r = requests.post(TRANSLATE_API_URL, json=payload, headers=headers, timeout=15)
        j = r.json()
        if isinstance(j, dict) and ("translatedText" in j):
            return j["translatedText"]
        # LibreTranslate sometimes returns {"translatedText": "..."}
        if "translatedText" in j:
            return j["translatedText"]
        # or an array/other format
        return j.get("translatedText") or j.get("data", {}).get("translations", [{}])[0].get("translatedText", "")
    except Exception as ex:
        print("Libre translate error:", ex)
        return text

def translate_deepl(text, target="RU"):
    try:
        key = DEEPL_API_KEY
        if not key:
            return text
        data = {"auth_key": key, "text": text, "target_lang": target}
        r = requests.post(DEEPL_API_URL, data=data, timeout=15)
        j = r.json()
        return j.get("translations", [{}])[0].get("text", text)
    except Exception as ex:
        print("DeepL translate error:", ex)
        return text

def translate_autodetect(text):
    if not TRANSLATE:
        return text
    # skip short texts
    if not text or len(text.strip()) < 12:
        return text
    # if already Russian, skip
    if looks_russian(text):
        return text
    # choose provider
    if TRANSLATE_PROVIDER == "deepl" and DEEPL_API_KEY:
        return translate_deepl(text)
    else:
        return translate_libre(text)
      # Build digest text (Russian)
def build_digest(items):
    lines = []
    lines.append("Дайджест: топ кейсов и новостей про нейросети и LLM")
    lines.append("Дата: " + datetime.utcnow().strftime("%Y-%m-%d"))
    lines.append("")
    for i, it in enumerate(items[:MAX_ITEMS], start=1):
        title = it.get("title") or it.get("link")
        summary = it.get("summary") or ""
        # translate if needed
        if title:
            title = translate_autodetect(title)
        if summary:
            summary = translate_autodetect(summary)
        else:
            # try extract
            txt = extract_first_sentence(it.get("link"))
            if txt:
                summary = translate_autodetect(txt)
        lines.append(f"{i}. {title}\n{summary}\n{it.get('link')}\n")
    return "\n".join(lines)

# Send to Telegram
def send_telegram(text):
    from telegram import Bot
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram credentials not set")
        return False
    bot = Bot(token=token)
    try:
        # Telegram has message length limits; split if > 4000 chars
        MAX_CH = 3900
        parts = [text[i:i+MAX_CH] for i in range(0, len(text), MAX_CH)]
        for p in parts:
            bot.send_message(chat_id=chat_id, text=p)
        return True
    except Exception as ex:
        print("Telegram send error:", ex)
        return False

def main():
    conn = init_db()
    items = []
    items += fetch_rss()
    time.sleep(0.5)
    items += fetch_newsapi()
    # dedupe
    uniq = {}
    for it in items:
        link = it.get("link")
        if not link:
            continue
        if link in uniq:
            if (not uniq[link].get("summary")) and it.get("summary"):
                uniq[link] = it
        else:
            uniq[link] = it
    items = list(uniq.values())

    # filter by keywords and unseen
    scored = []
    for it in items:
        link = it.get("link")
        if not link or is_seen(conn, link):
            continue
        s = score_item(it)
        if s > 0:
            scored.append((s, it))
    scored.sort(key=lambda x: (-x[0], x[1].get("published") or datetime.min))
    selected = [it for _, it in scored][:MAX_ITEMS]

    # Fallback: if ничего не нашлось, возьмём топ по дате (новые)
    if not selected:
        candidate = sorted(items, key=lambda x: x.get("published") or datetime.min, reverse=True)[:MAX_ITEMS]
        selected = [it for it in candidate if not is_seen(conn, it.get("link"))][:MAX_ITEMS]

    if not selected:
        print("No new items to send.")
        return

    for it in selected:
        mark_seen(conn, it.get("link"))

    text = build_digest(selected)
    ok = False
    if USE_TELEGRAM:
        ok = send_telegram(text)
    if ok:
        print("Sent")
    else:
        print("Send failed or no channel configured")

if __name__ == "__main__":
    main()
