import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from .errors import DiscoveryError, KairosError, VideoError
from .guard import timestamp

ATOM = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
ID = re.compile(r"^[\w-]{11}$")
CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")


class YouTube:
    def __init__(self, http, guard, api_key=""):
        self.http, self.guard, self.api_key = http, guard, api_key

    def api(self, resource, params):
        if not self.api_key:
            raise DiscoveryError("youtube_key_needed_for_gap_recovery")
        self.guard.reserve(provider="youtube", units=1)
        response = self.http.request(
            "GET",
            "https://www.googleapis.com/youtube/v3/"
            + resource
            + "?"
            + urlencode(params),
            headers={"x-goog-api-key": self.api_key},
        )
        if response.status == 403:
            raise DiscoveryError("youtube_auth_or_quota_failed")
        if response.status != 200:
            raise DiscoveryError()
        return response.json()

    def feed(self, cid):
        if not CHANNEL_ID.fullmatch(cid):
            raise DiscoveryError("invalid_channel_id")
        try:
            response = self.http.request(
                "GET", "https://www.youtube.com/feeds/videos.xml?channel_id=" + cid
            )
            if response.status != 200:
                raise DiscoveryError()
            root = ET.fromstring(response.body)
            root_id = root.findtext("yt:channelId", namespaces=ATOM)
            links = [
                link.get("href")
                for link in root.findall("a:link", ATOM)
                if link.get("rel") == "alternate"
            ]
            # Official feeds currently omit the UC prefix at feed level, while
            # entry-level IDs and canonical links retain it.
            if (
                root_id not in (cid, cid[2:])
                or "https://www.youtube.com/channel/" + cid not in links
            ):
                raise DiscoveryError("feed_channel_mismatch")
            videos = []
            for entry in root.findall("a:entry", ATOM):
                if entry.findtext("yt:channelId", namespaces=ATOM) != cid:
                    raise DiscoveryError("feed_entry_channel_mismatch")
                vid = entry.findtext("yt:videoId", namespaces=ATOM)
                if not vid or not ID.fullmatch(vid):
                    raise DiscoveryError("invalid_video_id")
                published = entry.findtext("a:published", namespaces=ATOM)
                timestamp(published)
                videos.append(
                    {
                        "video_id": vid,
                        "title": entry.findtext("a:title", default="", namespaces=ATOM),
                        "published_at": published,
                        "channel_id": cid,
                        "url": "https://www.youtube.com/watch?v=" + vid,
                    }
                )
            return sorted(
                videos, key=lambda x: timestamp(x["published_at"]), reverse=True
            )
        except (ET.ParseError, ValueError, TypeError):
            raise DiscoveryError("invalid_feed") from None

    def uploads(self, cid, since):
        channels = self.api("channels", {"part": "contentDetails", "id": cid})
        try:
            playlist = channels["items"][0]["contentDetails"]["relatedPlaylists"][
                "uploads"
            ]
        except (KeyError, IndexError):
            raise DiscoveryError("uploads_playlist_unavailable") from None
        videos, token = [], None
        while True:
            params = {
                "part": "snippet,contentDetails",
                "playlistId": playlist,
                "maxResults": 50,
            }
            if token:
                params["pageToken"] = token
            result = self.api("playlistItems", params)
            past_cutoff = False
            for item in result.get("items", []):
                detail = item.get("contentDetails", {})
                vid, published = detail.get("videoId"), detail.get("videoPublishedAt")
                if not vid or not ID.fullmatch(vid) or not published:
                    continue
                if timestamp(published) < since:
                    past_cutoff = True
                    continue
                videos.append(
                    {
                        "video_id": vid,
                        "title": item["snippet"]["title"],
                        "published_at": published,
                        "channel_id": cid,
                        "url": "https://www.youtube.com/watch?v=" + vid,
                    }
                )
            token = result.get("nextPageToken")
            if past_cutoff or not token:
                return videos

    def metadata(self, video):
        """Read official public watch-page metadata. Never use title/description as AI content."""
        try:
            response = self.http.request("GET", video["url"])
            if response.status != 200:
                raise VideoError()
            page = response.body.decode("utf-8", errors="replace")
            match = re.search(r"(?:var\s+)?ytInitialPlayerResponse\s*=\s*", page)
            if not match:
                raise VideoError("video_metadata_unavailable")
            data, _ = json.JSONDecoder().raw_decode(page[match.end() :])
            details = data.get("videoDetails", {})
            if (
                details.get("videoId") != video["video_id"]
                or details.get("channelId") != video["channel_id"]
            ):
                raise VideoError("video_channel_mismatch")
            if data.get("playabilityStatus", {}).get("status") != "OK":
                raise VideoError("video_not_publicly_playable")
            if (
                details.get("isLive")
                or details.get("isLiveContent")
                and not details.get("lengthSeconds")
            ):
                raise VideoError("video_live_or_upcoming")
            micro = data.get("microformat", {}).get("playerMicroformatRenderer", {})
            if micro.get("isUnlisted") or details.get("isPrivate"):
                raise VideoError("video_not_public")
            seconds = int(details.get("lengthSeconds", 0))
            if seconds <= 0:
                raise VideoError("video_duration_unknown")
            return seconds
        except (ValueError, KeyError, TypeError):
            raise VideoError("video_metadata_invalid") from None


def discover(state, youtube, channels, clock):
    failures = []
    for channel in channels:
        now = clock()
        cid = channel.get("channel_id")
        if not cid or not channel.get("verification", "").startswith("verified"):
            failures.append("channel_unverified")
            state.error("channel_unverified", now, channel_id=cid)
            continue
        cursor = state.data["channels"].setdefault(
            cid, {"baseline": now.isoformat(), "last_success": None}
        )
        state.save()  # Baseline survives even if first discovery fails.
        cutoff = timestamp(cursor["last_success"] or cursor["baseline"])
        try:
            try:
                videos = youtube.feed(cid)
                gap = bool(videos) and timestamp(videos[-1]["published_at"]) > cutoff
            except KairosError:
                if not youtube.api_key:
                    raise
                videos, gap = youtube.uploads(cid, cutoff), False
            if gap and youtube.api_key:
                videos, gap = youtube.uploads(cid, cutoff), False
            for video in videos:
                # Always use channel baseline to prevent bootstrap history blasts.
                if timestamp(video["published_at"]) < timestamp(cursor["baseline"]):
                    continue
                if video["video_id"] not in state.data["videos"]:
                    state.data["videos"][video["video_id"]] = {
                        **video,
                        "priority": channel.get("priority", 50),
                        "discovered_at": now.isoformat(),
                        "status": "pending",
                        "attempts": 0,
                        "retry_at": None,
                        "parts": [],
                        "sent_parts": [],
                    }
            if gap:
                # Keep original cursor so a later API-enabled run can recover the gap.
                failures.append("feed_gap_youtube_key_required")
                state.error("feed_gap_youtube_key_required", now, channel_id=cid)
            else:
                cursor["last_success"] = now.isoformat()
            state.save()
        except KairosError as error:
            failures.append(error.code)
            state.error(error.code, now, channel_id=cid)
    return failures
