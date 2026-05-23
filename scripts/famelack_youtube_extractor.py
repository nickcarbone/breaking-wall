#!/usr/bin/env python3
"""
famelack_youtube_extractor.py
─────────────────────────────
Extracts YouTube Live channel links from the Famelack public dataset.

LICENSE NOTE: Famelack publishes their channel data at
  https://github.com/famelack/famelack-data
under the MIT License, which explicitly permits "use, copy, modify, remix,
and build on this data, including commercially." No scraping of famelack.com
is performed — this script only reads their published GitHub dataset.

Attribution (optional per MIT terms):
  Data sourced from Famelack (famelack.com)

USAGE:
  pip install requests
  python3 famelack_youtube_extractor.py

  # Filter to news category only (default):
  python3 famelack_youtube_extractor.py --category news

  # Get all YouTube links across all categories:
  python3 famelack_youtube_extractor.py --all-categories

  # Filter by country code (ISO 3166-1 alpha-2):
  python3 famelack_youtube_extractor.py --country us

  # Output as JSON instead of plain text:
  python3 famelack_youtube_extractor.py --format json

  # Save to file:
  python3 famelack_youtube_extractor.py --output my_streams.json --format json
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────

GITHUB_RAW_BASE = "https://raw.githubusercontent.com/famelack/famelack-data/main"

# All known categories in the Famelack dataset.
# The news category is the primary target for News Wall.
KNOWN_CATEGORIES = [
    "news",
    "entertainment",
    "sports",
    "music",
    "kids",
    "movies",
    "documentary",
    "religious",
    "business",
    "lifestyle",
    "education",
    "weather",
    "cooking",
    "science",
    "travel",
]

YOUTUBE_PATTERNS = re.compile(
    r"(youtube\.com/watch\?v=|youtube\.com/embed/|youtu\.be/|youtube\.com/live/)",
    re.IGNORECASE,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_json(url: str, retries: int = 3) -> list | dict | None:
    """Fetch a JSON file from GitHub raw with retry logic."""
    headers = {"User-Agent": "NewsWall/1.0 (famelack-data MIT dataset reader)"}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 404:
                return None  # file doesn't exist for this category/country
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # exponential backoff
            else:
                print(f"  WARNING: Failed to fetch {url}: {e}", file=sys.stderr)
                return None


def is_youtube_url(url: str) -> bool:
    """Return True if the URL is a YouTube Live / embed link."""
    return bool(YOUTUBE_PATTERNS.search(url or ""))


def normalize_youtube_url(url: str) -> str:
    """
    Normalize YouTube URLs to a consistent watch?v= format where possible.
    Embed and live URLs are left as-is since they may serve different purposes.
    """
    url = url.strip()
    # youtube.com/live/VIDEO_ID → watch?v=
    live_match = re.match(r"https?://(?:www\.)?youtube\.com/live/([A-Za-z0-9_-]+)", url)
    if live_match:
        return f"https://www.youtube.com/watch?v={live_match.group(1)}"
    return url


def extract_youtube_entries(data: list | dict) -> list[dict]:
    """
    Walk the Famelack JSON structure and return all entries with YouTube URLs.
    The schema may vary; this handles both list-of-channels and dict wrappers.
    """
    channels = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Famelack sometimes wraps the list under a key; try common ones
        for key in ("channels", "items", "data", "streams"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
        else:
            # Flatten all list values
            items = []
            for v in data.values():
                if isinstance(v, list):
                    items.extend(v)
    else:
        return []

    for entry in items:
        if not isinstance(entry, dict):
            continue

        # URL field may be under different keys
        url = entry.get("url") or entry.get("stream_url") or entry.get("stream") or ""
        if not is_youtube_url(url):
            continue

        channels.append({
            "name":     entry.get("name") or entry.get("channel") or "Unknown",
            "url":      normalize_youtube_url(url),
            "country":  entry.get("country") or entry.get("country_code") or "",
            "language": entry.get("language") or entry.get("lang") or "",
            "category": entry.get("category") or entry.get("categories") or "",
            "logo":     entry.get("logo") or entry.get("icon") or "",
        })

    return channels


# ── Core logic ────────────────────────────────────────────────────────────────

def fetch_category(category: str) -> list[dict]:
    """Fetch a single category file and return YouTube entries."""
    url = f"{GITHUB_RAW_BASE}/tv/raw/categories/{category}.json"
    print(f"  Fetching category: {category}...", file=sys.stderr)
    data = fetch_json(url)
    if data is None:
        return []
    entries = extract_youtube_entries(data)
    print(f"    → {len(entries)} YouTube streams found", file=sys.stderr)
    return entries


def fetch_country(country_code: str) -> list[dict]:
    """Fetch a single country file and return YouTube entries."""
    url = f"{GITHUB_RAW_BASE}/tv/raw/countries/{country_code.lower()}.json"
    print(f"  Fetching country: {country_code}...", file=sys.stderr)
    data = fetch_json(url)
    if data is None:
        print(f"    No data found for country code '{country_code}'", file=sys.stderr)
        return []
    entries = extract_youtube_entries(data)
    print(f"    → {len(entries)} YouTube streams found", file=sys.stderr)
    return entries


def deduplicate(entries: list[dict]) -> list[dict]:
    """Deduplicate by URL, keeping the first occurrence."""
    seen = set()
    out = []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            out.append(e)
    return out


# ── Output formatters ─────────────────────────────────────────────────────────

def output_plain(entries: list[dict], file=sys.stdout):
    """Simple newline-separated list of YouTube URLs."""
    for e in entries:
        print(e["url"], file=file)


def output_json(entries: list[dict], file=sys.stdout):
    """Full metadata as JSON array."""
    json.dump(entries, file, indent=2, ensure_ascii=False)
    print(file=file)


def output_table(entries: list[dict], file=sys.stdout):
    """Human-readable table."""
    col_name = max((len(e["name"]) for e in entries), default=10)
    col_name = min(col_name, 40)
    col_country = 7
    header = f"{'Name':<{col_name}}  {'Country':<{col_country}}  URL"
    print(header, file=file)
    print("-" * (len(header) + 20), file=file)
    for e in entries:
        name = e["name"][:col_name].ljust(col_name)
        country = (e["country"] or "")[:col_country].ljust(col_country)
        print(f"{name}  {country}  {e['url']}", file=file)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Extract YouTube Live links from Famelack's public MIT-licensed dataset."
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "--category", "-c",
        default="news",
        metavar="CATEGORY",
        help=f"Category to fetch (default: news). Options: {', '.join(KNOWN_CATEGORIES)}",
    )
    source.add_argument(
        "--all-categories",
        action="store_true",
        help="Fetch all known categories (slower, more results).",
    )
    source.add_argument(
        "--country",
        metavar="CODE",
        help="Fetch by ISO 3166-1 alpha-2 country code (e.g. us, gb, de).",
    )
    p.add_argument(
        "--format", "-f",
        choices=["plain", "json", "table"],
        default="table",
        help="Output format (default: table).",
    )
    p.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Write output to FILE instead of stdout.",
    )
    return p.parse_args()


def main():
    args = parse_args()

    print("Famelack YouTube Live Extractor", file=sys.stderr)
    print("Dataset: github.com/famelack/famelack-data (MIT License)", file=sys.stderr)
    print(file=sys.stderr)

    # ── Fetch ──────────────────────────────────────────────────────────────
    entries: list[dict] = []

    if args.all_categories:
        for cat in KNOWN_CATEGORIES:
            entries.extend(fetch_category(cat))
    elif args.country:
        entries = fetch_country(args.country)
    else:
        entries = fetch_category(args.category)

    entries = deduplicate(entries)
    print(f"\nTotal unique YouTube streams: {len(entries)}", file=sys.stderr)

    if not entries:
        print("No YouTube streams found. The schema may have changed — check the repo.", file=sys.stderr)
        sys.exit(1)

    # ── Output ─────────────────────────────────────────────────────────────
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            if args.format == "json":
                output_json(entries, file=f)
            elif args.format == "plain":
                output_plain(entries, file=f)
            else:
                output_table(entries, file=f)
        print(f"Output written to: {args.output}", file=sys.stderr)
    else:
        if args.format == "json":
            output_json(entries)
        elif args.format == "plain":
            output_plain(entries)
        else:
            output_table(entries)


if __name__ == "__main__":
    main()
