#!/usr/bin/env python3
"""
BREAKING WALL — Daily YouTube Live Link Crawler
=================================================
Crawls YouTube for live stream links from major global news sources of record.
Exports structured JSON with channel metadata, viewership, and canonical watch URLs.

Usage:
    python breaking_wall_crawler.py                    # Run with YouTube Data API
    python breaking_wall_crawler.py --no-api           # Run with yt-dlp fallback (no API key needed)
    python breaking_wall_crawler.py --output wall.json # Custom output path
    python breaking_wall_crawler.py --csv              # Also export CSV

Requires:
    pip install google-api-python-client yt-dlp requests

Set environment variable YOUTUBE_API_KEY to your YouTube Data API v3 key.
"""

import json
import csv
import os
import sys
import argparse
import subprocess
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("breaking_wall")

# Canonical watch URL template — the ONLY format we output
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

# ---------------------------------------------------------------------------
# GLOBAL NEWS SOURCE REGISTRY
# ---------------------------------------------------------------------------
# Each entry: (channel_id, display_name, location, relevance)
#
# Selection criteria:
#   1. Source of record — the outlet people in that country/region turn to first
#   2. High daily/weekly viewership on YouTube (validates global attention)
#   3. Maintains a persistent or frequently recurring YouTube live stream
#   4. Includes state media where it is the dominant source (CCTV, RT, etc.)
# ---------------------------------------------------------------------------

SOURCES = [
    # ── UNITED STATES ──────────────────────────────────────────────────────
    {
        "channel_id": "UCupvZG-5ko_eiXAupbDfxWw",
        "name": "CNN",
        "location": "United States",
        "relevance": "America's most-watched cable news network internationally; the default breaking-news channel for airports, hotels, and newsrooms worldwide."
    },
    {
        "channel_id": "UCXIJgqnII2ZOINSWNOGFThA",
        "name": "Fox News",
        "location": "United States",
        "relevance": "The highest-rated US cable news channel by domestic viewership; defines the conservative news agenda and drives political discourse."
    },
    {
        "channel_id": "UCaXkIU1QidjPwiAYu6GcHjg",
        "name": "MSNBC",
        "location": "United States",
        "relevance": "Leading US progressive cable news outlet; primary counterweight to Fox News in American political media."
    },
    {
        "channel_id": "UCBi2mrWuNuyYy4gbM6fU18Q",
        "name": "ABC News",
        "location": "United States",
        "relevance": "One of the US Big Three broadcast networks; consistently high-reach breaking news coverage with mainstream credibility."
    },
    {
        "channel_id": "UC8p1vwvWtl6T73JiExfWs1g",
        "name": "CBS News",
        "location": "United States",
        "relevance": "Historic US broadcast network with deep investigative tradition; 60 Minutes franchise and 24/7 streaming news."
    },
    {
        "channel_id": "UCeY0bbntWzzVIaj2z3QigXg",
        "name": "NBC News",
        "location": "United States",
        "relevance": "Flagship US broadcast news operation; home to Today, Nightly News, and Meet the Press — major agenda-setters."
    },
    {
        "channel_id": "UCIALMKvObZNtJ68-rmLjXhA",
        "name": "Bloomberg Television",
        "location": "United States",
        "relevance": "The global standard for financial markets coverage; real-time data-driven news watched by traders and policymakers."
    },
    {
        "channel_id": "UCvJJ_dzjViJCoLf5uKUTwoA",
        "name": "CNBC",
        "location": "United States",
        "relevance": "America's dominant business news network; sets the tone for market sentiment and corporate news globally."
    },
    {
        "channel_id": "UCaB_KyYOjfNHBm0f-TvBmiw",
        "name": "C-SPAN",
        "location": "United States",
        "relevance": "Unfiltered, gavel-to-gavel US government proceedings; the raw feed of American democracy without editorial overlay."
    },
    {
        "channel_id": "UCHqC-yWZ1kri4YzwRSt6RGQ",
        "name": "NewsNation",
        "location": "United States",
        "relevance": "Fast-growing US cable outlet positioning itself as a centrist alternative; increasingly significant in the American news landscape."
    },

    # ── UNITED KINGDOM ─────────────────────────────────────────────────────
    {
        "channel_id": "UC16niRr50-MSBwiO3YDb3RA",
        "name": "BBC News",
        "location": "United Kingdom",
        "relevance": "The world's most recognized news brand; the benchmark for broadcast journalism with unmatched global bureau network."
    },
    {
        "channel_id": "UCoMdktPbSTixAyNGwb-UYkQ",
        "name": "Sky News",
        "location": "United Kingdom",
        "relevance": "Britain's 24-hour rolling news pioneer; one of the most-watched free live news streams on YouTube globally."
    },
    {
        "channel_id": "UCBQ3TEq3R6WkQBfWmBO4GHw",
        "name": "GB News",
        "location": "United Kingdom",
        "relevance": "UK's newest major news channel, modeled on opinion-led formats; rapidly growing YouTube presence in British media."
    },

    # ── MIDDLE EAST ────────────────────────────────────────────────────────
    {
        "channel_id": "UCNye-wNBqNL5ZzHSJj3l8Bg",
        "name": "Al Jazeera English",
        "location": "Qatar",
        "relevance": "The most influential English-language news outlet from the Arab world; massive YouTube live viewership especially during conflict coverage."
    },
    {
        "channel_id": "UCpLsKrBfMBZqsMJXVxDBzpQ",
        "name": "Al Jazeera Arabic",
        "location": "Qatar",
        "relevance": "The Arabic-language original that reshaped Middle Eastern media; the primary news source for hundreds of millions of Arabic speakers."
    },
    {
        "channel_id": "UCbyBtNQhjBbGDPH1OaaCNEQ",
        "name": "Al Arabiya",
        "location": "United Arab Emirates / Saudi Arabia",
        "relevance": "Saudi-backed pan-Arab news network; the principal Gulf-aligned counterpoint to Al Jazeera in regional coverage."
    },
    {
        "channel_id": "UCKJPCPx3mMtSFVmZPYKL7eA",
        "name": "i24NEWS English",
        "location": "Israel",
        "relevance": "Israel's international English-language news channel; a primary source for the Israeli perspective on Middle East events."
    },

    # ── EUROPE ─────────────────────────────────────────────────────────────
    {
        "channel_id": "UCQfwfsi5VrQ8yKZ-UWmAEFg",
        "name": "France 24 English",
        "location": "France",
        "relevance": "France's international news channel; provides the Francophone and European continental perspective in English to a global audience."
    },
    {
        "channel_id": "UCCCPCZNChQdGa9EkATeye4g",
        "name": "France 24 Français",
        "location": "France",
        "relevance": "The French-language edition with enormous viewership across Francophone Africa and Europe; a key voice in the French-speaking world."
    },
    {
        "channel_id": "UCknLrEdhRCp1aegoMqRaCZg",
        "name": "DW News",
        "location": "Germany",
        "relevance": "Germany's international broadcaster; the primary English-language source for German and Central European perspectives."
    },
    {
        "channel_id": "UCQGqX5Ndpm4snE0NTjyOJnA",
        "name": "Euronews",
        "location": "France (Pan-European)",
        "relevance": "Europe's most-watched cross-border news channel; covers EU affairs and continental events from a multilingual, multinational newsroom."
    },

    # ── SOUTH ASIA ─────────────────────────────────────────────────────────
    {
        "channel_id": "UCYPvAwZP8pZhSMW8qs7cVCw",
        "name": "India Today",
        "location": "India",
        "relevance": "One of India's most-watched English news channels on YouTube; massive live viewership during elections and national events."
    },
    {
        "channel_id": "UCt4t-jeY85JegMlZ-E5UXtA",
        "name": "NDTV",
        "location": "India",
        "relevance": "India's most internationally recognized independent news outlet; seen as the benchmark for credible Indian English-language journalism."
    },
    {
        "channel_id": "UCRWFSbif-RFENbBrSiez1DA",
        "name": "Republic World",
        "location": "India",
        "relevance": "High-viewership Indian English news channel known for aggressive editorial style; significant YouTube live audience."
    },
    {
        "channel_id": "UCz2kYMEoEl5kec6sGRKmCQA",
        "name": "Times Now",
        "location": "India",
        "relevance": "India's Times Group flagship English news channel; consistently among the top-rated Indian news operations by viewership."
    },
    {
        "channel_id": "UC_gUM8rL-Lrg6O3adPW9K1g",
        "name": "WION",
        "location": "India",
        "relevance": "India's fastest-growing international news channel; positions itself as a non-Western global perspective and has built a huge YouTube following."
    },
    {
        "channel_id": "UCRq3PMAfiMDG_-DjMXnmmMg",
        "name": "Geo News",
        "location": "Pakistan",
        "relevance": "Pakistan's most-watched news channel; the dominant source for Pakistani political and national affairs coverage."
    },

    # ── EAST ASIA ──────────────────────────────────────────────────────────
    {
        "channel_id": "UCgrNz-aDmcr2uuto8_DL2jg",
        "name": "CGTN",
        "location": "China",
        "relevance": "China's English-language state broadcaster; the official channel through which Beijing communicates its narrative to the world."
    },
    {
        "channel_id": "UCnL8MaGMwEAHR7NY1VJDCqA",
        "name": "CCTV Video News Agency",
        "location": "China",
        "relevance": "China Central Television's international feed; the primary state media organ of the world's second-largest economy."
    },
    {
        "channel_id": "UCo3wLJiqFQVlZa8pjINmjuQ",
        "name": "NHK World-Japan",
        "location": "Japan",
        "relevance": "Japan's national public broadcaster's international service; the authoritative English-language source on Japanese affairs."
    },
    {
        "channel_id": "UCF5Bvo0MQ6SV0oiGxkaXxwQ",
        "name": "Arirang TV",
        "location": "South Korea",
        "relevance": "South Korea's international English-language broadcaster; key source for Korean Peninsula developments and East Asian affairs."
    },
    {
        "channel_id": "UC3k3jnBznVUPyxijiaYRyGg",
        "name": "CNA (Channel NewsAsia)",
        "location": "Singapore",
        "relevance": "Singapore's premier English-language news channel; the most trusted name in Southeast Asian news coverage."
    },

    # ── RUSSIA / FORMER SOVIET ─────────────────────────────────────────────
    {
        "channel_id": "UCpwvZwUam-Ur-4vwGSGFsFg",
        "name": "RT",
        "location": "Russia",
        "relevance": "Russia's state-funded international broadcaster; regardless of editorial stance, it is the Kremlin's primary global media instrument and widely watched."
    },

    # ── TURKEY ─────────────────────────────────────────────────────────────
    {
        "channel_id": "UCRWjkMeA3pOhULMaaPJvJBg",
        "name": "TRT World",
        "location": "Turkey",
        "relevance": "Turkey's English-language state broadcaster; projects Ankara's perspective on regional and global affairs."
    },

    # ── AFRICA ─────────────────────────────────────────────────────────────
    {
        "channel_id": "UCCXwY5Rch-4EAqUEOiLkBSg",
        "name": "Channels Television",
        "location": "Nigeria",
        "relevance": "Nigeria's leading independent news network; the most credible live broadcast source from Africa's most populous nation."
    },
    {
        "channel_id": "UCIfAm2pQncPNyxk8S5B3h5A",
        "name": "eNCA",
        "location": "South Africa",
        "relevance": "South Africa's most-watched English news channel; the primary live source for Southern African affairs and politics."
    },
    {
        "channel_id": "UCH-5LCxV9mBsB0W_kU5QXYA",
        "name": "KTN News Kenya",
        "location": "Kenya",
        "relevance": "Kenya's leading TV news operation; the go-to live source for East African politics and regional developments."
    },

    # ── LATIN AMERICA ──────────────────────────────────────────────────────
    {
        "channel_id": "UCFr_FaEamxNyHkpOQzubJig",
        "name": "TeleSUR English",
        "location": "Venezuela (Pan–Latin America)",
        "relevance": "Latin America's multilateral news network backed by several governments; the primary left-aligned pan-regional news voice."
    },
    {
        "channel_id": "UCEGx26b0j3se-bXDqukPwgQ",
        "name": "Globo News",
        "location": "Brazil",
        "relevance": "The 24-hour news arm of Brazil's Globo media empire; the most powerful news brand in Latin America's largest country."
    },
    {
        "channel_id": "UCXmAOGbFnDSMOil-NaPRE0w",
        "name": "Todo Noticias (TN)",
        "location": "Argentina",
        "relevance": "Argentina's most-watched cable news channel; dominant source for Argentine political and economic developments."
    },

    # ── AUSTRALIA / OCEANIA ────────────────────────────────────────────────
    {
        "channel_id": "UCs5Y5_7XK8HLDX0SLNwkd3w",
        "name": "ABC News Australia",
        "location": "Australia",
        "relevance": "Australia's national public broadcaster; the most trusted news source in Australia with growing international YouTube reach."
    },
    {
        "channel_id": "UC4JCksJF76g_MdzPVBJoC3Q",
        "name": "Sky News Australia",
        "location": "Australia",
        "relevance": "Australia's 24-hour cable news network; one of the highest-performing news channels on YouTube globally by engagement."
    },
]


# ---------------------------------------------------------------------------
# METHOD 1 — YOUTUBE DATA API v3 (preferred)
# ---------------------------------------------------------------------------

def crawl_with_api(api_key: str) -> list[dict]:
    """Use YouTube Data API v3 to find live streams and pull metadata."""
    try:
        from googleapiclient.discovery import build
    except ImportError:
        log.error("google-api-python-client not installed. Run: pip install google-api-python-client")
        sys.exit(1)

    youtube = build("youtube", "v3", developerKey=api_key)
    results = []

    for source in SOURCES:
        channel_id = source["channel_id"]
        name = source["name"]
        log.info(f"Checking: {name} ({channel_id})")

        try:
            # --- Step 1: Search for live broadcasts on this channel -----------
            search_resp = youtube.search().list(
                part="id,snippet",
                channelId=channel_id,
                eventType="live",
                type="video",
                order="viewCount",
                maxResults=3
            ).execute()

            live_items = search_resp.get("items", [])
            if not live_items:
                log.info(f"  ⊘ No live stream found for {name}")
                results.append(_build_entry(source, live=False))
                continue

            # Take the top live stream (highest view count)
            video_id = live_items[0]["id"]["videoId"]
            snippet = live_items[0]["snippet"]

            # --- Step 2: Get detailed video stats ----------------------------
            video_resp = youtube.videos().list(
                part="statistics,liveStreamingDetails,snippet",
                id=video_id
            ).execute()

            video_data = video_resp["items"][0] if video_resp.get("items") else {}
            stats = video_data.get("statistics", {})
            live_details = video_data.get("liveStreamingDetails", {})

            concurrent_viewers = int(live_details.get("concurrentViewers", 0))
            total_views = int(stats.get("viewCount", 0))

            # --- Step 3: Get channel-level subscriber count ------------------
            channel_resp = youtube.channels().list(
                part="statistics",
                id=channel_id
            ).execute()
            ch_stats = channel_resp["items"][0]["statistics"] if channel_resp.get("items") else {}
            subscribers = int(ch_stats.get("subscriberCount", 0))

            results.append(_build_entry(
                source,
                live=True,
                video_id=video_id,
                stream_title=snippet.get("title", ""),
                concurrent_viewers=concurrent_viewers,
                total_views=total_views,
                subscribers=subscribers,
            ))

            log.info(f"  ✓ LIVE — {WATCH_URL.format(video_id=video_id)}  ({concurrent_viewers:,} watching)")

        except Exception as e:
            log.warning(f"  ✗ Error checking {name}: {e}")
            results.append(_build_entry(source, live=False, error=str(e)))

    return results


# ---------------------------------------------------------------------------
# METHOD 2 — YT-DLP FALLBACK (no API key needed)
# ---------------------------------------------------------------------------

def crawl_with_ytdlp() -> list[dict]:
    """Use yt-dlp to check each channel's /live page for active streams."""
    results = []

    for source in SOURCES:
        channel_id = source["channel_id"]
        name = source["name"]
        channel_url = f"https://www.youtube.com/channel/{channel_id}/live"
        log.info(f"Checking: {name} via yt-dlp")

        try:
            proc = subprocess.run(
                [
                    "yt-dlp",
                    "--dump-json",
                    "--no-download",
                    "--no-playlist",
                    channel_url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if proc.returncode != 0:
                log.info(f"  ⊘ No live stream found for {name}")
                results.append(_build_entry(source, live=False))
                continue

            data = json.loads(proc.stdout)
            video_id = data.get("id", "")
            is_live = data.get("is_live", False)

            if not is_live:
                log.info(f"  ⊘ Channel page returned non-live content for {name}")
                results.append(_build_entry(source, live=False))
                continue

            concurrent = data.get("concurrent_view_count", 0) or 0
            view_count = data.get("view_count", 0) or 0

            results.append(_build_entry(
                source,
                live=True,
                video_id=video_id,
                stream_title=data.get("title", ""),
                concurrent_viewers=concurrent,
                total_views=view_count,
                subscribers=data.get("channel_follower_count", 0) or 0,
            ))

            log.info(f"  ✓ LIVE — {WATCH_URL.format(video_id=video_id)}  ({concurrent:,} watching)")

        except subprocess.TimeoutExpired:
            log.warning(f"  ✗ Timeout for {name}")
            results.append(_build_entry(source, live=False, error="timeout"))
        except Exception as e:
            log.warning(f"  ✗ Error checking {name}: {e}")
            results.append(_build_entry(source, live=False, error=str(e)))

    return results


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _build_entry(
    source: dict,
    live: bool,
    video_id: str = "",
    stream_title: str = "",
    concurrent_viewers: int = 0,
    total_views: int = 0,
    subscribers: int = 0,
    error: str = "",
) -> dict:
    entry = {
        "channel_name": source["name"],
        "channel_id": source["channel_id"],
        "location": source["location"],
        "relevance": source["relevance"],
        "is_live": live,
        "watch_url": WATCH_URL.format(video_id=video_id) if video_id else None,
        "video_id": video_id or None,
        "stream_title": stream_title or None,
        "concurrent_viewers": concurrent_viewers,
        "total_stream_views": total_views,
        "channel_subscribers": subscribers,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if error:
        entry["error"] = error
    return entry


def export_json(results: list[dict], path: str):
    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_sources": len(results),
            "live_now": sum(1 for r in results if r["is_live"]),
            "offline": sum(1 for r in results if not r["is_live"]),
        },
        "sources": sorted(results, key=lambda r: (not r["is_live"], -r["concurrent_viewers"])),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info(f"Exported JSON → {path}")


def export_csv(results: list[dict], path: str):
    fieldnames = [
        "channel_name", "location", "is_live", "watch_url",
        "concurrent_viewers", "total_stream_views", "channel_subscribers",
        "stream_title", "relevance", "checked_at",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(results, key=lambda r: (not r["is_live"], -r["concurrent_viewers"])):
            writer.writerow(row)
    log.info(f"Exported CSV → {path}")


def print_summary(results: list[dict]):
    live = [r for r in results if r["is_live"]]
    offline = [r for r in results if not r["is_live"]]

    print("\n" + "=" * 72)
    print(f"  BREAKING WALL — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  {len(live)} sources LIVE  ·  {len(offline)} offline")
    print("=" * 72)

    if live:
        print("\n  LIVE NOW:")
        print("  " + "-" * 68)
        for r in sorted(live, key=lambda x: -x["concurrent_viewers"]):
            viewers = f"{r['concurrent_viewers']:>8,} watching" if r["concurrent_viewers"] else "   viewers n/a"
            print(f"  {r['channel_name']:<28} {r['location']:<24} {viewers}")
            print(f"    → {r['watch_url']}")
        print()

    if offline:
        print(f"\n  OFFLINE ({len(offline)} sources — no live stream detected):")
        print("  " + "-" * 68)
        for r in sorted(offline, key=lambda x: x["channel_name"]):
            print(f"  {r['channel_name']:<28} {r['location']}")
    print()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Breaking Wall — YouTube Live News Crawler")
    parser.add_argument("--output", "-o", default="breaking_wall_links.json", help="JSON output path")
    parser.add_argument("--csv", action="store_true", help="Also export CSV")
    parser.add_argument("--no-api", action="store_true", help="Use yt-dlp instead of YouTube Data API")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress per-channel log output")
    args = parser.parse_args()

    if args.quiet:
        log.setLevel(logging.WARNING)

    api_key = os.environ.get("YOUTUBE_API_KEY", "")

    if args.no_api:
        log.info("Running with yt-dlp fallback (no API key)")
        results = crawl_with_ytdlp()
    elif api_key:
        log.info("Running with YouTube Data API v3")
        results = crawl_with_api(api_key)
    else:
        log.warning("No YOUTUBE_API_KEY found. Falling back to yt-dlp.")
        log.warning("Set YOUTUBE_API_KEY for better results and higher reliability.")
        results = crawl_with_ytdlp()

    # Export
    export_json(results, args.output)
    if args.csv:
        csv_path = args.output.rsplit(".", 1)[0] + ".csv"
        export_csv(results, csv_path)

    # Console summary
    print_summary(results)

    print(f"  Output: {args.output}")
    if args.csv:
        print(f"  CSV:    {csv_path}")
    print()


if __name__ == "__main__":
    main()
