import argparse
import getpass
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import KairosError, PolicyError
from .gemini import Gemini
from .guard import (
    Guard,
    fingerprint,
    load_confirmation,
    timestamp,
    validate_policy,
    verify_free_tier,
)
from .http import Http
from .runner import run
from .state import GitCheckpoint, State, exclusive_lock, git
from .telegram import Telegram
from .youtube import YouTube

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc)


def policy():
    result = json.loads((ROOT / "config/policy.json").read_text(encoding="utf-8"))
    validate_policy(result)
    return result


def channels():
    # JSON is a strict subset of YAML 1.2, avoiding an extra runtime dependency.
    rows = json.loads((ROOT / "config/channels.yaml").read_text(encoding="utf-8"))[
        "channels"
    ]
    if len(rows) != 32 or len({r["url"] for r in rows}) != 32:
        raise PolicyError("channel_count_invalid")
    return rows


def telegram(http):
    token, cid = (
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    return Telegram(http, token, cid) if token and cid else None


def state(args, config):
    checkpoint = (
        GitCheckpoint(args.state, config["max_git_storage_bytes"])
        if args.git_state
        else None
    )
    if args.git_state:
        sizes, _ = git("count-objects", "-v")
        stats = dict(line.split(": ") for line in sizes.splitlines())
        size = (int(stats.get("size", 0)) + int(stats.get("size-pack", 0))) * 1024
        if size >= config["max_git_storage_bytes"]:
            raise PolicyError("git_storage_limit_reached")
    return State(args.state, checkpoint=checkpoint, max_bytes=config["max_state_bytes"])


def confirm(args):
    if (
        args.tier != "free"
        or args.billing_disabled != "CONFIRMED"
        or args.old_key_revoked != "CONFIRMED"
    ):
        raise PolicyError()
    if any(
        n <= 0
        for n in (
            args.rpm,
            args.tpm,
            args.rpd,
            args.tokens_per_day,
            args.video_seconds_per_day,
        )
    ):
        raise PolicyError("actual_free_limits_required")
    key = getpass.getpass("Rotated Gemini key (hidden, never logged): ")
    record = {
        "project_id": args.project,
        "tier": "free",
        "billing_enabled": False,
        "key_rotated": True,
        "confirmed_at": now().isoformat(),
        "expires_at": (now() + timedelta(days=7)).isoformat(),
        "model": policy()["model"],
        "key_sha256": fingerprint(key),
        "dedicated_project": args.dedicated_project == "CONFIRMED",
        "rpm": args.rpm,
        "tpm": args.tpm,
        "rpd": args.rpd,
        "tokens_per_day": args.tokens_per_day,
        "youtube_video_seconds_per_day": min(28800, args.video_seconds_per_day),
        "source": "User verified AI Studio Free Tier, Cloud Billing disabled, old key revoked, actual rate limits",
    }
    key = None
    path = ROOT / "state/free-tier-confirmation.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(
        "Confirmation written to state/free-tier-confirmation.json (no API key). Valid 7 days."
    )


def probe(args):
    """Public-only diagnostics; no credential reads and no messages."""
    config = policy()
    s = State(ROOT / "state/probe.json")
    guard = Guard(s, config, {}, now)
    yt = YouTube(Http(), guard)
    results = []
    if args.inspect_feed:
        resp = yt.http.request(
            "GET",
            "https://www.youtube.com/feeds/videos.xml?channel_id="
            + channels()[0]["channel_id"],
        )
        print(repr(resp.body[:1800]))
        return 0
    for row in channels():
        try:
            feed = yt.feed(row["channel_id"])
            result = {
                "channel_id": row["channel_id"],
                "name": row["name"],
                "feed_entries": len(feed),
                "status": "ok",
            }
            if (
                args.sample_duration
                and not any("sample_duration_seconds" in r for r in results)
                and feed
            ):
                result["sample_video_id"] = feed[0]["video_id"]
                result["sample_duration_seconds"] = yt.metadata(feed[0])
        except KairosError as error:
            result = {"channel_id": row["channel_id"], "status": error.code}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    path = ROOT / "docs/public-probe.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {"at": now().isoformat(), "results": results}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    return int(any(r["status"] != "ok" for r in results))


def watchdog(args, s, http):
    repo, token = (
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GH_TOKEN", ""),
    )
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not token:
        raise PolicyError("github_monitor_credentials_missing")
    response = http.request(
        "GET",
        f"https://api.github.com/repos/{repo}/actions/workflows/youtube.yml/runs?per_page=50",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
        },
    )
    if response.status != 200:
        raise PolicyError("github_monitor_read_failed")
    runs = response.json().get("workflow_runs", [])
    recent = [
        r
        for r in runs
        if r.get("conclusion") == "success"
        and r.get("event") in ("schedule", "workflow_dispatch")
    ]
    stale = not recent or now() - timestamp(recent[0]["created_at"]) > timedelta(
        hours=14  # Longest scheduled gap is 12h, plus 2h delay allowance.
    )
    failed = bool(
        runs and runs[0].get("conclusion") in ("failure", "timed_out", "cancelled")
    )
    if stale or failed:
        s.error("github_schedule_failed_or_stale", now())
        tg = telegram(http)
        if tg:
            from .runner import send_briefing

            send_briefing(
                s,
                tg,
                now(),
                {
                    "sent": 0,
                    "pending": sum(
                        v["status"] != "sent" for v in s.data["videos"].values()
                    ),
                    "delivery_uncertain": 0,
                    "errors": ["github_schedule_failed_or_stale"],
                    "reason": "github_schedule_failed_or_stale",
                },
                namespace="watchdog",
            )
        print("github_schedule_failed_or_stale")
        return 2
    print("github_schedule_recent_success")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Kairos YouTube Intelligence 4.3 — zero additional cost"
    )
    parser.add_argument("--state", type=Path, default=ROOT / "state/state.json")
    parser.add_argument(
        "--git-state",
        action="store_true",
        help="Durably checkpoint to isolated state branch",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("discover")
    sub.add_parser("status")
    sub.add_parser("watchdog")
    p = sub.add_parser("probe")
    p.add_argument("--sample-duration", action="store_true")
    p.add_argument("--inspect-feed", action="store_true")
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--video-url", required=True)
    smoke.add_argument(
        "--retry-pending",
        action="store_true",
        help="Explicit retry after a reviewed non-auth/non-quota smoke failure; quota checks remain enforced",
    )
    smoke.add_argument(
        "--metadata-file",
        type=Path,
        help="Recent explicit public metadata snapshot for this single test only",
    )
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--video-id", required=True)
    reconcile.add_argument(
        "--message-id",
        type=int,
        required=True,
        help="Verify the uncertain part in your own Telegram chat first",
    )
    c = sub.add_parser("confirm-free-tier")
    c.add_argument("--project", required=True)
    c.add_argument("--tier", required=True)
    c.add_argument("--billing-disabled", required=True)
    c.add_argument("--old-key-revoked", required=True)
    c.add_argument("--dedicated-project", default="UNVERIFIED")
    for flag in ("rpm", "tpm", "rpd", "tokens-per-day", "video-seconds-per-day"):
        c.add_argument("--" + flag, type=int, required=True)
    args = parser.parse_args()
    if args.command == "confirm-free-tier":
        confirm(args)
        return 0
    if args.command == "probe":
        return probe(args)
    with exclusive_lock(args.state.with_suffix(".lock")):
        return execute(args)


def execute(args):
    config, http = policy(), Http()
    s = state(args, config)
    if args.command == "status":
        print(
            json.dumps(
                {
                    "statuses": dict(
                        Counter(v["status"] for v in s.data["videos"].values())
                    ),
                    "channels": len(s.data["channels"]),
                    "errors": s.data["errors"][-10:],
                    "paused": bool(s.data["pause"]),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "watchdog":
        return watchdog(args, s, http)
    if args.command == "reconcile":
        video = s.data["videos"].get(args.video_id)
        if not video or video["status"] != "delivery_uncertain" or args.message_id <= 0:
            raise PolicyError("delivery_reconciliation_invalid")
        video["sent_parts"].append(args.message_id)
        video["status"] = (
            "sent" if len(video["sent_parts"]) == len(video["parts"]) else "ready"
        )
        s.save()
        print("delivery_reconciled")
        return 0
    confirmation, key = (
        load_confirmation(),
        os.environ.get("GEMINI_API_KEY", "").strip(),
    )
    guard = Guard(s, config, confirmation, now)
    yt = YouTube(http, guard, os.environ.get("YOUTUBE_API_KEY", ""))
    model = Gemini(http, guard, config, key)
    rows = channels()
    if args.command == "smoke":
        # All checks BEFORE inserting test work or contacting Google.
        verify_free_tier(config, confirmation, key, now())
        if telegram(http) is None:
            raise PolicyError("telegram_secrets_missing_or_invalid")
        match = re.fullmatch(
            r"https://www\.youtube\.com/watch\?v=([\w-]{11})", args.video_url
        )
        if not match:
            raise PolicyError("smoke_requires_canonical_public_video_url")
        vid = match.group(1)
        if args.metadata_file and not yt.api_key:
            yt.verified_metadata = json.loads(
                args.metadata_file.read_text(encoding="utf-8")
            )
        try:
            details = yt.video_details(vid)
            cid = details["channelId"]
            if cid not in {r["channel_id"] for r in rows}:
                raise PolicyError("smoke_video_channel_not_registered")
            s.data["videos"].setdefault(
                vid,
                {
                    "video_id": vid,
                    "channel_id": cid,
                    "title": details["title"],
                    "url": args.video_url,
                    "published_at": now().isoformat(),
                    "discovered_at": now().isoformat(),
                    "priority": 1000,
                    "status": "pending",
                    "attempts": 0,
                    "parts": [],
                    "sent_parts": [],
                    "retry_at": None,
                },
            )
            if args.retry_pending:
                target = s.data["videos"][vid]
                if target["status"] != "pending" or not (
                    target.get("last_error", "").startswith("state_checkpoint_failed")
                    or target.get("last_error")
                    in (
                        "invalid_video_evidence",
                        "invalid_summary_schema",
                        "invalid_summary_length",
                        "invalid_summary_bullet",
                        "insufficient_video_coverage",
                        "gemini_incomplete",
                    )
                ):
                    raise PolicyError("smoke_retry_requires_reviewed_content_failure")
                target["retry_at"] = None
            s.save()
        except (KeyError, ValueError):
            raise PolicyError("smoke_video_metadata_invalid") from None
    result = run(
        s,
        rows,
        yt,
        model,
        telegram(http),
        config,
        confirmation,
        key,
        now,
        discovery_only=args.command == "discover",
        max_videos=1 if args.command == "smoke" else None,
        only_video_id=vid if args.command == "smoke" else None,
    )
    print(json.dumps(result, ensure_ascii=False))
    smoke_incomplete = (
        args.command == "smoke" and s.data["videos"][vid]["status"] != "sent"
    )
    return int(
        bool(
            result.get("reason")
            or result.get("errors")
            or result.get("processing_errors")
            or smoke_incomplete
        )
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KairosError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        # Tracebacks may include request URLs containing Telegram tokens.
        print('{"error":"unexpected_failure_details_redacted"}', file=sys.stderr)
        raise SystemExit(3) from None
