"""Resolve handles from official public YouTube pages, without any API key."""

import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kairos_youtube.http import Http

URLS = """https://www.youtube.com/channel/UCaWi2foADm_lKAKnmeQwLSA
https://www.youtube.com/@yeomvely
https://www.youtube.com/@daishintv
https://www.youtube.com/@moneymoneycomics
https://www.youtube.com/@syukaworld
https://www.youtube.com/@hodolryinvest
https://www.youtube.com/@ystreet
https://www.youtube.com/@3protv
https://www.youtube.com/@understanding.
https://www.youtube.com/channel/UCA_hgsFzmynpv1zkvA5A7jA
https://www.youtube.com/@lucky_tv
https://www.youtube.com/@chesleytv
https://www.youtube.com/@turbo832tv
https://www.youtube.com/@jachung__
https://www.youtube.com/@3.3campus
https://www.youtube.com/@ezkipost-p4o
https://www.youtube.com/@godofit_official
https://www.youtube.com/channel/UCh2r2uIuiAreVV9Nfr_JhVA
https://www.youtube.com/@honeycomtv
https://www.youtube.com/@theelec9812
https://www.youtube.com/@meritzon
https://www.youtube.com/@official_myohada
https://www.youtube.com/@mildam21
https://www.youtube.com/@user-charlesmililab
https://www.youtube.com/@sosumonkey
https://www.youtube.com/channel/UCiYbaVEODktcsh09454Grow
https://www.youtube.com/@supe-tv
https://www.youtube.com/@unrealtech
https://www.youtube.com/@apkwon_
https://www.youtube.com/@eng_tv
https://www.youtube.com/@koreanstockrider
https://www.youtube.com/@ttimestv""".splitlines()


def resolve(url, http):
    response = http.request("GET", url)
    if response.status != 200:
        return None, None, f"http_{response.status}"
    page = response.body.decode("utf-8", errors="replace")
    patterns = [
        r'<meta\s+itemprop="channelId"\s+content="(UC[\w-]{22})"',
        r'"channelMetadataRenderer"\s*:\s*\{\s*"title"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*"description"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*"rssUrl"\s*:\s*"https://www.youtube.com/feeds/videos.xml\?channel_id=(UC[\w-]{22})"',
        r'<link[^>]+rel="canonical"[^>]+href="https://www.youtube.com/channel/(UC[\w-]{22})"',
        r'"externalId"\s*:\s*"(UC[\w-]{22})"',
    ]
    found = next((m.group(1) for p in patterns if (m := re.search(p, page))), None)
    title = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', page)
    return (
        found,
        html.unescape(title.group(1)) if title else url.rsplit("/", 1)[-1],
        "verified_public_channel_page" if found else "unresolved",
    )


def main():
    path = Path(__file__).resolve().parents[1] / "config/channels.yaml"
    existing = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"channels": []}
    )
    prior = {c["url"]: c for c in existing["channels"]}
    channels = []
    http = Http()
    for i, url in enumerate(URLS, 1):
        old = prior.get(url, {})
        try:
            cid, name, status = resolve(url, http)
        except Exception:
            cid, name, status = None, None, "network_error"
        direct = re.search(r"/channel/(UC[\w-]{22})$", url)
        channel = {
            "url": url,
            "channel_id": cid
            or old.get("channel_id")
            or (direct.group(1) if direct else None),
            "name": name or old.get("name") or url.rsplit("/", 1)[-1],
            "priority": old.get("priority", 50),
            "verification": status if cid else old.get("verification", status),
            "verified_at": datetime.now(timezone.utc).isoformat()
            if cid
            else old.get("verified_at"),
        }
        channels.append(channel)
        print(
            f"{i:02d} {url.rsplit('/', 1)[-1]} {channel['channel_id']} {channel['verification']}",
            flush=True,
        )
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {"channels": channels + [prior[u] for u in URLS[i:] if u in prior]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return (
        0
        if all(
            c["channel_id"] and c["verification"].startswith("verified")
            for c in channels
        )
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
