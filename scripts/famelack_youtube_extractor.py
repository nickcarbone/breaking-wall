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
  python3 famelack_youtube_extractor.py --category news --format json --output candidates.json
"""

import argparse
import json
import re
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)

GITHUB_RAW_BASE = "https://raw.githubusercontent.com/famelack/famelack-data/main"

KNOWN_CATEGORIES = [
    "news", "entertainment", "sports", "music", "kids",
    "movies", "documentary", "religious", "business",
    "lifestyle", "education", "weather", "cooking", "science", "travel",
]

def fetch_json(url, retries=3):
    headers = {"User-Agent": "NewsWall/1.0 (famelack-data MIT dataset reader)"}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  WARNING: Failed to fetch {url}: {e}", file=sys.stderr)
                return None

def normalize_youtube_url(url):
    """
    Convert youtube-nocookie.com/embed/VIDEO_ID
    to standard https://www.youtube.com/watch?v=VIDEO_ID
    """
    url = url.strip()
    match = re.search(r"(?:youtube\.com|youtube-nocookie\.com)/embed/([A-Za-z0-9_-]+)", url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    match = re.search(r"(?:youtube\.com)/live/([A-Za-z0-9_-]+)", url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return url

def extract_youtube_entries(data):
    """
    Famelack schema (as of May 2026):
      - name:         string
      - youtube_urls: list of youtube-nocookie.com/embed/VIDEO_ID strings
      - stream_urls:  list of direct IPTV m3u8 URLs (we ignore these)
      - languages:    list of ISO 639-3 language codes
      - country:      ISO 3166-1 alpha-2 country code
      - isGeoBlocked: bool
      - nanoid:       internal ID
    """
    if not isinstance(data, list):
        print("  WARNING: Unexpected data format (not a list)", file=sys.stderr)
        return []

    entries = []
    for item in data:
        youtube_urls = item.get("youtube_urls") or []
        if not youtube_urls:
            continue

        # Use the first YouTube URL (channels rarely have more than one)
        raw_url = youtube_urls[0]
        normalized_url = normalize_youtube_url(raw_url)

        languages = item.get("languages") or []
        language = languages[0] if languages else ""

        entries.append({
            "name":     item.get("name", "Unknown"),
            "url":      normalized_url,
            "country":  item.get("country", ""),
            "language": language,
            "region":   "",   # not in Famelack schema; fill in manually or via mapping
            "favorite": False,
            "dead":     False,
        })

    return entries

def fetch_category(category):
    url = f"{GITHUB_RAW_BASE}/tv/raw/categories/{category}.json"
    print(f"  Fetching category: {category}...", file=sys.stderr)
    data = fetch_json(url)
    if data is None:
        return []
    entries = extract_youtube_entries(data)
    print(f"    → {len(entries)} YouTube streams found", file=sys.stderr)
    return entries

def fetch_country(country_code):
    url = f"{GITHUB_RAW_BASE}/tv/raw/countries/{country_code.lower()}.json"
    print(f"  Fetching country: {country_code}...", file=sys.stderr)
    data = fetch_json(url)
    if data is None:
        print(f"    No data found for country code '{country_code}'", file=sys.stderr)
        return []
    entries = extract_youtube_entries(data)
    print(f"    → {len(entries)} YouTube streams found", file=sys.stderr)
    return entries

def deduplicate(entries):
    seen = set()
    out = []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            out.append(e)
    return out

def parse_args():
    p = argparse.ArgumentParser(description="Extract YouTube Live links from Famelack's MIT-licensed dataset.")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--category", "-c", default="news", metavar="CATEGORY",
        help=f"Category to fetch (default: news).")
    source.add_argument("--all-categories", action="store_true",
        help="Fetch all known categories.")
    source.add_argument("--country", metavar="CODE",
        help="Fetch by ISO 3166-1 alpha-2 country code (e.g. us, gb, de).")
    p.add_argument("--format", "-f", choices=["json", "plain"], default="json",
        help="Output format (default: json).")
    p.add_argument("--output", "-o", metavar="FILE",
        help="Write output to FILE instead of stdout.")
    return p.parse_args()

def main():
    args = parse_args()

    print("Famelack YouTube Live Extractor", file=sys.stderr)
    print("Dataset: github.com/famelack/famelack-data (MIT License)", file=sys.stderr)
    print(file=sys.stderr)

    entries = []
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
        print("No YouTube streams found. Check the repo schema.", file=sys.stderr)
        sys.exit(1)

    output_text = json.dumps(entries, indent=2, ensure_ascii=False) if args.format == "json" else "\n".join(e["url"] for e in entries)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_text)
        print(f"Output written to: {args.output}", file=sys.stderr)
    else:
        print(output_text)

if __name__ == "__main__":
    main()
