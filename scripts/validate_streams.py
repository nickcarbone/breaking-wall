#!/usr/bin/env python3
"""
validate_streams.py
-------------------
Keeps news_sources.json healthy for the Breaking Wall. news_sources.json is
the master list: the wall reads it on every load, so whatever this script
writes is what you see.

Run manually:    YOUTUBE_API_KEY=... python scripts/validate_streams.py
Run via Actions: .github/workflows/validate-streams.yml (daily, and on every
                 manual edit of news_sources.json, and from the Actions tab)

What one run does
  1. Checks every source in batches of 50 (videos.list, 1 quota unit per 50).
  2. Works out a status for each: live, scheduled, offline, dead, or blocked
     (embedding disabled, or region-blocked for VIEWER_COUNTRY).
  3. For anything not playable, looks for a replacement on the same channel:
       a. cheap: the channel's uploads playlist (about 2 units), then
       b. expensive: search.list eventType=live (100 units), at most once per
          source every SEARCH_COOLDOWN_HOURS and MAX_SEARCHES_PER_RUN per run.
  4. Decides how the wall should embed each live source. A channel that is
     running exactly ONE live broadcast (the one on file) gets
     embed:"channel", and the wall then embeds it by channel ID, so YouTube
     picks the current broadcast at page load: always fresh, no quota, no
     key in the page. A channel running several live broadcasts at once
     keeps its specific video, because a channel embed would pick one of
     them unpredictably.
  5. Writes news_sources.json ONLY if something meaningful changed, so the
     repo history records real events instead of a timestamp every run.
  6. Writes alert.md listing anything still unresolved, with likely
     replacements from candidates.json matched on country and name. The
     workflow turns that into a GitHub issue (and closes it when all clear).

Exit codes: 0 = ran fine (whether or not things are dead), 1 = hard failure.

Quota per run at 50 sources: about 70 units, plus 100 per search (at most
MAX_SEARCHES_PER_RUN). The free allowance is 10,000 units/day.
"""

import json
import os
import re
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Configuration ──────────────────────────────────────────────────────────────

SOURCES_FILE = Path("news_sources.json")
CANDIDATES_FILE = Path("candidates.json")
ALERT_FILE = Path("alert.md")

YT_API_BASE = "https://www.googleapis.com/youtube/v3"
API_DELAY = 0.2

# The country you watch from. A stream region-blocked here counts as blocked.
VIEWER_COUNTRY = "US"

SEARCH_COOLDOWN_HOURS = 20     # a daily run can always search; manual re-runs can't burn quota
MAX_SEARCHES_PER_RUN = 10

# Fields that change every run and carry no editorial meaning. They are
# stripped from the file so they never cause a commit.
VOLATILE_FIELDS = {"last_checked", "concurrent_viewers"}

PLAYABLE = {"live"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Small helpers ──────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def extract_video_id(url: str):
    if not url:
        return None
    m = re.search(r"(?:[?&]v=|youtu\.be/|/live/|/embed/)([A-Za-z0-9_-]{6,15})", url)
    return m.group(1) if m else None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def slugify(text: str) -> str:
    s = re.sub(r"[^\w]+", "-", (text or "").lower(), flags=re.UNICODE).strip("-")
    return s or "source"


def make_key(source: dict, taken: set) -> str:
    """Stable identity for a source. Written once, never changed, so the
    wall's saved order/favourites/hides survive URL rotations and channel_id
    backfills."""
    base = f"{slugify(source.get('name'))}-{(source.get('country') or 'xx').lower()}"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    return key


def api_get(endpoint: str, params: dict, api_key: str):
    params = {**params, "key": api_key}
    r = requests.get(f"{YT_API_BASE}/{endpoint}", params=params, timeout=15)
    r.raise_for_status()
    time.sleep(API_DELAY)
    return r.json()


def fetch_videos(video_ids, api_key):
    """videos.list in batches of 50. Returns (items_by_id, failed_ids)."""
    items, failed = {}, set()
    ids = [v for v in dict.fromkeys(video_ids) if v]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            data = api_get("videos", {
                "id": ",".join(chunk),
                "part": "snippet,liveStreamingDetails,status,contentDetails",
                "maxResults": 50,
            }, api_key)
        except requests.RequestException as e:
            log.warning(f"videos.list failed for a batch of {len(chunk)}: {e}")
            failed.update(chunk)
            continue
        for item in data.get("items", []):
            items[item["id"]] = item
    return items, failed


def classify(item) -> tuple:
    """Return (status, reason) for one videos.list item (or None)."""
    if item is None:
        return "dead", "video no longer exists"

    snippet = item.get("snippet", {})
    live = item.get("liveStreamingDetails", {})
    status = item.get("status", {})
    restriction = item.get("contentDetails", {}).get("regionRestriction", {})

    if status.get("privacyStatus") == "private":
        return "dead", "video is private"
    if live.get("actualEndTime"):
        return "dead", "broadcast ended"
    if status.get("embeddable") is False:
        return "blocked", "embedding disabled by the broadcaster"
    blocked = [c.upper() for c in restriction.get("blocked", [])]
    allowed = [c.upper() for c in restriction.get("allowed", [])]
    if VIEWER_COUNTRY in blocked or (allowed and VIEWER_COUNTRY not in allowed):
        return "blocked", f"region-blocked in {VIEWER_COUNTRY}"

    if live.get("actualStartTime") or snippet.get("liveBroadcastContent") == "live":
        return "live", None
    if live.get("scheduledStartTime") or snippet.get("liveBroadcastContent") == "upcoming":
        return "scheduled", None
    return "offline", "not broadcasting"


_uploads_cache = {}


def recent_uploads(channel_id, api_key):
    """Most recent video ids on a channel's uploads playlist (UCxxxx ->
    UUxxxx). 1 quota unit. Cached for the run."""
    if channel_id in _uploads_cache:
        return _uploads_cache[channel_id]
    ids = []
    if channel_id and channel_id.startswith("UC"):
        try:
            data = api_get("playlistItems", {
                "playlistId": "UU" + channel_id[2:],
                "part": "contentDetails",
                "maxResults": 15,
            }, api_key)
            ids = [it["contentDetails"]["videoId"] for it in data.get("items", [])]
        except requests.RequestException as e:
            log.info(f"  uploads playlist unavailable for {channel_id}: {e}")
    _uploads_cache[channel_id] = ids
    return ids


def find_live_on_channel(channel_id, api_key, allow_search, exclude):
    """Look for a playable live broadcast on a channel. Returns
    (video_id or None, used_search: bool)."""
    candidate_ids = recent_uploads(channel_id, api_key)

    def first_playable(ids):
        ids = [v for v in ids if v and v not in exclude]
        if not ids:
            return None
        items, _ = fetch_videos(ids, api_key)
        for vid in ids:
            item = items.get(vid)
            if item and item.get("snippet", {}).get("channelId") == channel_id \
                    and classify(item)[0] == "live":
                return vid
        return None

    found = first_playable(candidate_ids)
    if found or not allow_search:
        return found, False

    # b) search.list. 100 units, so throttled by the caller.
    try:
        data = api_get("search", {
            "channelId": channel_id,
            "type": "video",
            "eventType": "live",
            "part": "id",
            "maxResults": 5,
        }, api_key)
        ids = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
    except requests.RequestException as e:
        log.warning(f"  search failed for {channel_id}: {e}")
        return None, True
    return first_playable(ids), True


# ── Candidate suggestions for the alert ────────────────────────────────────────

GENERIC_TOKENS = {"tv", "news", "live", "channel", "noticias", "en", "vivo",
                  "the", "24", "24h", "hd", "online", "official", "de", "la"}


def name_tokens(name):
    return {t for t in re.findall(r"\w+", (name or "").lower()) if t not in GENERIC_TOKENS}


def suggest_candidates(source, candidates, wall_ids, limit=3):
    country = (source.get("country") or "").lower()
    mine = name_tokens(source.get("name"))
    scored = []
    for c in candidates:
        if (c.get("country") or "").lower() != country:
            continue
        vid = extract_video_id(c.get("url", ""))
        if not vid or vid in wall_ids:
            continue
        overlap = len(mine & name_tokens(c.get("name")))
        if overlap:
            scored.append((overlap, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:limit]]


def write_alert(unresolved, candidates, wall_ids):
    if not unresolved:
        ALERT_FILE.write_text("", encoding="utf-8")
        return
    lines = [
        "## Streams that need a new link",
        "",
        "The validator could not find a working replacement for these on their own channels.",
        "Possible replacements come from `candidates.json` (Famelack), matched on country and name only,",
        "so check each one is the same broadcaster before using it.",
        "",
    ]
    for s in unresolved:
        lines.append(f"### {s['name']} (`{s.get('country', '?')}`, {s.get('language', '?')})")
        lines.append(f"- Status: **{s['status']}** — {s.get('status_reason') or 'unknown'}")
        lines.append(f"- Current link: {s.get('url')}")
        if not s.get("channel_id"):
            lines.append("- No `channel_id` on file, so automatic replacement is impossible until one is added.")
        sugg = suggest_candidates(s, candidates, wall_ids)
        if sugg:
            lines.append("- Possible replacements:")
            for c in sugg:
                lines.append(f"  - {c.get('name')} — {c.get('url')}")
        else:
            lines.append("- No match in candidates.json.")
        lines.append("")
    lines.append("*Kept up to date by the daily validator. This issue closes itself when everything is playable again.*")
    ALERT_FILE.write_text("\n".join(lines), encoding="utf-8")


def decide_embed_modes(sources, api_key):
    """embed:"channel" only where we can SEE that the channel has exactly one
    live broadcast and it is the one on file. Anything we can't confirm
    (playlist unavailable, several lives, the live video not visible in the
    uploads playlist) stays on its specific video id, which is the safe
    default and what the wall did before."""
    live_sources = [s for s in sources if s.get("status") == "live" and s.get("channel_id")]
    live_ids = {id(s) for s in live_sources}
    uploads = {s["channel_id"]: recent_uploads(s["channel_id"], api_key) for s in live_sources}
    all_ids = [v for ids in uploads.values() for v in ids]
    items, _ = fetch_videos(all_ids, api_key) if all_ids else ({}, set())

    for s in sources:
        before = s.get("embed")
        mode = None
        if id(s) in live_ids:
            cid = s["channel_id"]
            lives = [v for v in uploads.get(cid, [])
                     if (it := items.get(v)) and it.get("snippet", {}).get("channelId") == cid
                     and classify(it)[0] == "live"]
            current = extract_video_id(s.get("url", ""))
            if len(lives) == 1 and lives[0] == current:
                mode = "channel"
        if mode:
            s["embed"] = mode
        else:
            s.pop("embed", None)
        if s.get("embed") != before:
            log.info(f"[{s.get('name')}] embed mode → {s.get('embed', 'video')}")


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        log.error("YOUTUBE_API_KEY is not set (repo Settings → Secrets → Actions).")
        sys.exit(1)
    if not SOURCES_FILE.exists():
        log.error(f"Sources file not found: {SOURCES_FILE}")
        sys.exit(1)

    original_text = SOURCES_FILE.read_text(encoding="utf-8")
    sources = json.loads(original_text)
    if not isinstance(sources, list):
        log.error("news_sources.json must be a JSON array")
        sys.exit(1)
    log.info(f"Loaded {len(sources)} sources")

    candidates = []
    if CANDIDATES_FILE.exists():
        try:
            candidates = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("candidates.json is not valid JSON — skipping suggestions")

    # Identity + strip volatile fields
    taken = {s["key"] for s in sources if s.get("key")}
    for s in sources:
        for f in VOLATILE_FIELDS:
            s.pop(f, None)
        if not s.get("key"):
            s["key"] = make_key(s, taken)
            taken.add(s["key"])

    video_ids = {id(s): extract_video_id(s.get("url", "")) for s in sources}
    items, failed = fetch_videos(video_ids.values(), api_key)
    if failed and not items:
        log.error("Every videos.list request failed (bad key or quota exhausted?)")
        sys.exit(1)

    run_time = now_utc()
    searches_used = 0
    wall_ids = {v for v in video_ids.values() if v}

    for s in sources:
        name = s.get("name", "Unknown")
        vid = video_ids[id(s)]

        if not vid:
            s["status"], s["status_reason"] = "dead", "could not read a video id from the URL"
            log.warning(f"[{name}] invalid URL: {s.get('url')}")
            continue
        if vid in failed:
            log.warning(f"[{name}] API error — keeping last known status")
            continue

        item = items.get(vid)
        status, reason = classify(item)
        if item and not s.get("channel_id"):
            s["channel_id"] = item["snippet"].get("channelId")
            log.info(f"[{name}] backfilled channel_id {s['channel_id']}")

        if status in PLAYABLE:
            s["status"] = status
            s.pop("status_reason", None)
            s.pop("searched_at", None)
            if s.get("replacement_found") is False:
                s.pop("replacement_found")
            continue

        # Not playable: try the same channel for a working broadcast.
        channel_id = s.get("channel_id")
        new_vid = None
        if channel_id:
            last = s.get("searched_at")
            cooled = (not last) or (run_time - datetime.fromisoformat(last) >= timedelta(hours=SEARCH_COOLDOWN_HOURS))
            allow_search = status in {"dead", "blocked"} and cooled and searches_used < MAX_SEARCHES_PER_RUN
            new_vid, used_search = find_live_on_channel(channel_id, api_key, allow_search, exclude={vid})
            if used_search:
                searches_used += 1
                s["searched_at"] = run_time.isoformat(timespec="seconds")

        if new_vid:
            log.info(f"[{name}] {status} ({reason}) → replaced with {new_vid}")
            s["dead_url"] = s.get("url")
            s["url"] = watch_url(new_vid)
            s["status"] = "live"
            s["replacement_found"] = True
            s.pop("status_reason", None)
            s.pop("searched_at", None)
            wall_ids.add(new_vid)
        else:
            s["status"], s["status_reason"] = status, reason
            if status in {"dead", "blocked"}:
                s["replacement_found"] = False
            log.info(f"[{name}] {status.upper()} — {reason}")

    decide_embed_modes(sources, api_key)

    unresolved = [s for s in sources if s.get("status") in {"dead", "blocked"}]
    write_alert(unresolved, candidates, wall_ids)

    new_text = json.dumps(sources, ensure_ascii=False, indent=2) + "\n"
    if new_text != original_text:
        SOURCES_FILE.write_text(new_text, encoding="utf-8")
        log.info("news_sources.json updated")
    else:
        log.info("No meaningful changes — file left untouched")

    counts = {}
    for s in sources:
        counts[s.get("status", "?")] = counts.get(s.get("status", "?"), 0) + 1
    log.info(f"Summary: {counts} · searches used: {searches_used} · unresolved: {len(unresolved)}")
    set_output("unresolved", len(unresolved))


if __name__ == "__main__":
    main()
