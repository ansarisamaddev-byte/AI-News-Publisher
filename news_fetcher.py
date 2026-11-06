
import csv
import hashlib
import json
import re
import requests
import feedparser
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urldefrag
from calendar import timegm

BASE_DIR = Path(__file__).resolve().parent
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
NEWS_DIR = BASE_DIR / "data" / "news"

HOURS_LIMIT = 24
MAX_WORKERS = 10
TIMEOUT = 20
IST = ZoneInfo("Asia/Kolkata")

CSV_FIELDS = [
    "news_id", "title", "description", "url", "source", "country",
    "published_at", "category", "subcategory",
    "importance_score", "relevance_score", "selected_channels",
    "classified", "ai_done", "image_done", "post_done"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsCollector/1.0)"
}


def load_sources():
    if not SOURCES_FILE.exists():
        raise FileNotFoundError(f"Sources file not found: {SOURCES_FILE}")

    with open(SOURCES_FILE, "r", encoding="utf-8") as file:
        config = json.load(file)

    sources = config.get("google_news", []) + config.get("direct_sources", [])
    valid_sources = []

    for source in sources:
        if not isinstance(source, dict):
            continue

        name = str(source.get("name", "")).strip()
        url = str(source.get("url", "")).strip()

        if name and is_valid_url(url):
            valid_sources.append({"name": name, "url": url})

    return valid_sources

def extract_description(entry):
    candidates = [
        entry.get("summary"),
        entry.get("description"),
        entry.get("summary_detail", {}).get("value")
    ]

    # RSS content:encoded or Atom content.
    for content in entry.get("content", []):
        candidates.append(content.get("value", ""))

    for candidate in candidates:
        description = clean_text(candidate)
        if description:
            return description

    return ""

def is_valid_url(url):
    try:
        parsed = urlparse(str(url).strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def clean_text(value):
    if not value:
        return ""

    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(entry):
    # Prefer the original publication date over the updated date.
    for field in ("published_parsed", "updated_parsed"):
        value = entry.get(field)

        if value:
            try:
                return datetime.fromtimestamp(
                    timegm(value),
                    timezone.utc
                )
            except (TypeError, ValueError, OverflowError):
                pass

    for field in ("published", "updated", "pubDate"):
        value = entry.get(field)

        if not value:
            continue

        try:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                parsed = date_parser.parse(value)

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

        except (TypeError, ValueError, OverflowError):
            continue

    return None


def generate_news_id(url):
    normalized_url = urldefrag(url.strip())[0].rstrip("/")
    return hashlib.sha256(
        normalized_url.encode("utf-8")
    ).hexdigest()[:16]


def fetch_feed(source):
    name = source["name"]
    url = source["url"]

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT
        )
        response.raise_for_status()

        feed = feedparser.parse(response.content)
        articles = []

        for entry in feed.entries:
            title = clean_text(entry.get("title"))
            article_url = str(entry.get("link", "")).strip()

            if not title or not is_valid_url(article_url):
                continue

            description = extract_description(entry)
            published = parse_date(entry)

            if not published:
                continue

            articles.append({
                "news_id": generate_news_id(article_url),
                "title": title,
                "description": description,
                "url": article_url,
                "source": name,
                "country": "",
                "published_at": published.isoformat(),
                "published_datetime": published,
                "category": "",
                "subcategory": "",
                "importance_score": "",
                "relevance_score": "",
                "selected_channels": "",
                "classified": "false",
                "ai_done": "false",
                "image_done": "false",
                "post_done": "false"
            })

        if getattr(feed, "bozo", False):
            print(f"[WARNING] {name}: Feed contains parsing issues")

        print(f"[SUCCESS] {name}: {len(articles)} articles")
        return articles

    except requests.RequestException as error:
        print(f"[ERROR] {name}: Request failed - {error}")
        return []

    except Exception as error:
        print(f"[ERROR] {name}: {error}")
        return []


def get_daily_file():
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return NEWS_DIR / f"{today}.csv"


def normalize_existing_row(row):
    normalized = {}

    for field in CSV_FIELDS:
        value = row.get(field, "")
        normalized[field] = value.strip() if isinstance(value, str) else value

    return normalized


def load_existing_news():
    existing_rows = []
    existing_ids = set()

    today = datetime.now(IST).date()

    files = [
        NEWS_DIR / f"{(today - timedelta(days=1)).strftime('%Y-%m-%d')}.csv",
        NEWS_DIR / f"{today.strftime('%Y-%m-%d')}.csv"
    ]

    daily_file = get_daily_file()

    for file_path in files:
        if not file_path.exists():
            continue

        try:
            with open(file_path, "r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)

                for row in reader:
                    news_id = str(row.get("news_id", "")).strip()

                    if not news_id:
                        continue

                    existing_ids.add(news_id)

                    # Only today's records are copied into today's CSV.
                    if file_path == daily_file and news_id not in {
                        item["news_id"] for item in existing_rows
                    }:
                        existing_rows.append(normalize_existing_row(row))

        except (OSError, csv.Error) as error:
            print(f"[ERROR] Could not read {file_path.name}: {error}")

    return existing_rows, existing_ids


def save_news(rows, queue_file):
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = queue_file.with_suffix(".tmp")

    try:
        with open(temp_file, "w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=CSV_FIELDS,
                extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)

        temp_file.replace(queue_file)

    except Exception:
        if temp_file.exists():
            temp_file.unlink()
        raise


def fetch_latest_news():
    queue_file = get_daily_file()
    sources = load_sources()
    all_articles = []

    print(f"\n[START] Fetching news from {len(sources)} feeds")
    print(f"[DAILY CSV] {queue_file}\n")

    if sources:
        with ThreadPoolExecutor(
            max_workers=min(MAX_WORKERS, len(sources))
        ) as executor:
            futures = [
                executor.submit(fetch_feed, source)
                for source in sources
            ]

            for future in as_completed(futures):
                try:
                    all_articles.extend(future.result())
                except Exception as error:
                    print(f"[ERROR] Feed worker failed: {error}")

    # Calculate the time window after fetching feeds.
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_LIMIT)

    print(f"\n[TIME] From {cutoff.isoformat()} to {now.isoformat()}")

    # Accept only articles published within the exact last 24 hours.
    recent_articles = [
        article for article in all_articles
        if cutoff <= article["published_datetime"] <= now
    ]

    # Remove duplicate articles, keeping the newest version.
    unique_articles = {}

    for article in sorted(
        recent_articles,
        key=lambda item: item["published_datetime"],
        reverse=True
    ):
        news_id = article["news_id"]

        if news_id not in unique_articles:
            unique_articles[news_id] = article

    existing_rows, existing_ids = load_existing_news()

    # Prevent duplicates from today and yesterday.
    new_articles = []

    for article in unique_articles.values():
        if article["news_id"] in existing_ids:
            continue

        article.pop("published_datetime", None)
        new_articles.append(article)

    # Preserve all existing records and their processing statuses.
    final_rows = new_articles + existing_rows

    if new_articles or not queue_file.exists():
        save_news(final_rows, queue_file)

    print("\n========== NEWS COLLECTION SUMMARY ==========")
    print(f"Total feeds checked: {len(sources)}")
    print(f"Total articles fetched: {len(all_articles)}")
    print(f"Articles within 24 hours: {len(recent_articles)}")
    print(f"Unique recent articles: {len(unique_articles)}")
    print(f"New articles added: {len(new_articles)}")
    print(f"Today's CSV records: {len(final_rows)}")
    print(f"Yesterday/today IDs checked: {len(existing_ids)}")
    print(f"CSV saved: {queue_file}")
    print("=============================================\n")


def main():
    fetch_latest_news()


if __name__ == "__main__":
    main()
