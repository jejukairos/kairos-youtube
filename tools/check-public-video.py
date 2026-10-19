"""Public YouTube metadata diagnostics. No secrets or Google API calls."""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kairos_youtube.http import Http

http = Http()
urls = [
    "https://www.youtube.com/watch?v=TORQkD6cwUw",
    "https://www.youtube.com/watch?v=TORQkD6cwUw&hl=en",
    "https://m.youtube.com/watch?v=TORQkD6cwUw",
    "https://www.youtube.com/embed/TORQkD6cwUw",
]
for index, url in enumerate(urls):
    try:
        response = http.request("GET", url, timeout=20)
        page = response.body.decode("utf-8", errors="replace")
        match = re.search(r"(?:var\s+)?ytInitialPlayerResponse\s*=\s*", page)
        data = json.JSONDecoder().raw_decode(page[match.end() :])[0] if match else {}
        details = data.get("videoDetails", {})
        print(
            json.dumps(
                {
                    "variant": index,
                    "http_status": response.status,
                    "player_status": data.get("playabilityStatus", {}).get("status"),
                    "duration_seconds": details.get("lengthSeconds"),
                    "video_id_matches": details.get("videoId") == "TORQkD6cwUw",
                    "has_duration_meta": bool(re.search(r'itemprop="duration"', page)),
                }
            ),
            flush=True,
        )
    except Exception:
        print(
            json.dumps({"variant": index, "error": "public_metadata_unavailable"}),
            flush=True,
        )
