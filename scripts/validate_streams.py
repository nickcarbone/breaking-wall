#!/usr/bin/env python3
"""
validate_streams.py
-------------------
Validates YouTube live stream links for the Breaking Wall news wall.
Uses the YouTube Data API v3 (free, ~1 unit per video checked).

Run manually:    python validate_streams.py
Run via Actions: triggered by schedule in .github/workflows/validate-streams.yml

Requires environment variable: YOUTUBE_API_KEY
Store as a GitHub Secret named YOUTUBE_API_KEY — never hardcode it.

API quota cost per run: 1 unit per video check + 100 units per search query.
At 50 sources, a full check costs ~50 units. Daily quota is 10,000 units.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
import requests

# ── Configuration ──────────────────────────────────────────────────────────────

# Path to your sources JSON file (relative to repo root)
SOURCES_FILE = Path("news_sources.json")

# How many seconds to wait between API calls (be polite to the API)
API_DELAY = 0.3

# YouTube Data API base URL
YT_API_BASE = "https://www.googleapis.com/youtube/v3"

# If a video is "dead" (deleted/privated), attempt to find a replacement
# by searching the channel's live streams. Costs 100 API units per search.
AUTO_SEARCH_REPLACEMENTS = True

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> str | None:
    """Extract the video ID from a YouTube watch URL."""
    if not url:
        return None
    if "watch?v=" in url:
        return url.split("watch?v=")[1].split("&")[0]
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0]
    return None


def check_video_status(video_id: str, api_key: str) -> dict:
    """
    Query the YouTube API for a video's live status.

    Returns a dict with:
        status: "live" | "scheduled" | "offline" | "dead"
        channel_id: str | None
        title: str | None
        concurrent_viewers: int | None
    """
    url = f"{YT_API_BASE}/videos"
    params = {
        "id": video_id,
        "part": "snippet,liveStreamingDetails,statistics",
        "key": api_key,
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning(f"API request failed for {video_id}: {e}")
        return {"status": "error", "channel_id": None, "title": None, "concurrent_viewers": None}

    items = data.get("items", [])
    if not items:
        # Video doesn't exist — deleted, privated, or ID changed
        return {"status": "dead", "channel_id": None, "title": None, "concurrent_viewers": None}

    item = items[0]
    snippet = item.get("snippet", {})
    live = item.get("liveStreamingDetails", {})

    channel_id = snippet.get("channelId")
    title = snippet.get("title")
    concurrent_viewers = None

    # Determine live status
    if live.get("actualEndTime"):
        # Stream has ended — this video ID is no longer useful
        status = "dead"
    elif live.get("actualStartTime") and not live.get("actualEndTime"):
        # Stream started and hasn't ended — currently live
        status = "live"
        concurrent_viewers = int(live.get("concurrentViewers", 0)) or None
    elif live.get("scheduledStartTime") and not live.get("actualStartTime"):
        # Stream is scheduled but not yet live
        status = "scheduled"
    elif snippet.get("liveBroadcastContent") == "live":
        # Fallback check via snippet field
        status = "live"
    elif snippet.get("liveBroadcastContent") == "upcoming":
        status = "scheduled"
    else:
        # Video exists but is not currently live (could be intermittent offline)
        status = "offline"

    return {
        "status": status,
        "channel_id": channel_id,
        "title": title,
        "concurrent_viewers": concurrent_viewers,
    }


def search_for_live_stream(channel_id: str, api_key: str) -> str | None:
    """
    Search a channel for an active live stream.
    Costs 100 API units. Only called when a link is confirmed dead.
    Returns a video ID if found, else None.
    """
    url = f"{YT_API_BASE}/search"
    params = {
        "channelId": channel_id,
        "type": "video",
        "eventType": "live",
        "part": "id,snippet",
        "maxResults": 1,
        "key": api_key,
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning(f"Search failed for channel {channel_id}: {e}")
        return None

    items = data.get("items", [])
    if items:
        return items[0]["id"]["videoId"]
    return None


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        log.error("YOUTUBE_API_KEY environment variable is not set.")
        log.error("Add it as a GitHub Secret or export it locally before running.")
        sys.exit(1)

    if not SOURCES_FILE.exists():
        log.error(f"Sources file not found: {SOURCES_FILE}")
        sys.exit(1)

    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        sources = json.load(f)

    log.info(f"Loaded {len(sources)} sources from {SOURCES_FILE}")

    run_time = datetime.now(timezone.utc).isoformat()
    changed = 0
    dead_count = 0
    live_count = 0
    offline_count = 0

    for source in sources:
        name = source.get("name", "Unknown")
        url = source.get("url", "")
        video_id = extract_video_id(url)

        if not video_id:
            log.warning(f"[{name}] Could not parse video ID from URL: {url}")
            source["status"] = "invalid_url"
            source["last_checked"] = run_time
            continue

        log.info(f"[{name}] Checking {video_id}...")
        result = check_video_status(video_id, api_key)
        status = result["status"]

        # Store channel_id if we got one and don't have it yet
        if result["channel_id"] and not source.get("channel_id"):
            source["channel_id"] = result["channel_id"]
            changed += 1

        # Update concurrent viewers if live
        if result["concurrent_viewers"] is not None:
            source["concurrent_viewers"] = result["concurrent_viewers"]
        elif "concurrent_viewers" in source:
            # Clear stale viewer count if no longer live
            del source["concurrent_viewers"]

        previous_status = source.get("status")

        if status == "dead":
            dead_count += 1
            log.warning(f"[{name}] ❌ DEAD — video {video_id} no longer exists")
            source["status"] = "dead"
            source["dead_url"] = url  # preserve for reference

            # Attempt to find a replacement if we have a channel_id
            channel_id = result.get("channel_id") or source.get("channel_id")
            if AUTO_SEARCH_REPLACEMENTS and channel_id:
                log.info(f"[{name}] Searching channel {channel_id} for live stream...")
                time.sleep(API_DELAY)
                new_video_id = search_for_live_stream(channel_id, api_key)
                if new_video_id:
                    new_url = f"https://www.youtube.com/watch?v={new_video_id}"
                    log.info(f"[{name}] ✅ Found replacement: {new_url}")
                    source["url"] = new_url
                    source["status"] = "live"
                    source["replacement_found"] = True
                    changed += 1
                else:
                    log.warning(f"[{name}] No live replacement found on channel")
                    source["replacement_found"] = False
            elif not channel_id:
                log.warning(f"[{name}] No channel_id stored — cannot search for replacement. Add it manually.")

        elif status == "live":
            live_count += 1
            log.info(f"[{name}] ✅ LIVE ({result.get('concurrent_viewers', '?')} viewers)")
            source["status"] = "live"

        elif status == "offline":
            offline_count += 1
            log.info(f"[{name}] ⏸  OFFLINE (stream exists but not currently broadcasting)")
            source["status"] = "offline"

        elif status == "scheduled":
            log.info(f"[{name}] 🕐 SCHEDULED")
            source["status"] = "scheduled"

        elif status == "error":
            log.warning(f"[{name}] ⚠️  API error — skipping status update")
            # Don't overwrite last known status on transient errors

        if status != previous_status:
            changed += 1

        source["last_checked"] = run_time
        time.sleep(API_DELAY)

    # Write updated JSON back
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)

    log.info("─" * 60)
    log.info(f"Run complete at {run_time}")
    log.info(f"  Live:    {live_count}")
    log.info(f"  Offline: {offline_count}")
    log.info(f"  Dead:    {dead_count}")
    log.info(f"  Changes: {changed}")
    log.info(f"  Saved:   {SOURCES_FILE}")

    # Exit with non-zero code if there are dead links with no replacement
    # This makes GitHub Actions flag the run for review
    unresolved_dead = [
        s["name"] for s in sources
        if s.get("status") == "dead" and not s.get("replacement_found")
    ]
    if unresolved_dead:
        log.warning(f"Unresolved dead links: {', '.join(unresolved_dead)}")
        sys.exit(2)  # exit 2 = warnings, not a hard failure


if __name__ == "__main__":
    main()
