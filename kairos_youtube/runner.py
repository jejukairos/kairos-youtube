import time
from datetime import timedelta

from .errors import (
    AuthError,
    KairosError,
    PolicyError,
    QuotaError,
    TelegramError,
    TelegramRateLimit,
)
from .guard import fingerprint, timestamp, verify_free_tier
from .schedule import boundary, initialize_window
from .telegram import deliver, render
from .youtube import discover


def run(
    state,
    channels,
    youtube,
    gemini,
    telegram,
    policy,
    confirmation,
    key,
    clock,
    *,
    discovery_only=False,
    max_videos=None,
    only_video_id=None,
    require_youtube_api=False,
    skip_discovery=False,
):
    started, monotonic = clock(), time.monotonic()
    state.recover()
    if skip_discovery and only_video_id is None:
        raise PolicyError("manual_request_requires_one_video")
    first_run, window_end = None, None
    if skip_discovery or only_video_id is not None:
        failures = []
    else:
        first_run = initialize_window(state, channels, policy)
        window_end = boundary(started)
        if first_run is not None and started < first_run:
            # Before the requested first briefing, configure the baseline only.
            # No YouTube, Gemini or Telegram calls and no cursor advancement.
            return {
                "mode": "discovery_only" if discovery_only else "waiting_first_run",
                "discovered": len(state.data["videos"]),
                "errors": [],
                "first_run": first_run.isoformat(),
                "collection_initial_start": state.data["collection"]["initial_start"],
            }
        failures = discover(state, youtube, channels, clock, window_end=window_end)
    reason = None
    if discovery_only:
        return {
            "discovered": len(state.data["videos"]),
            "errors": failures,
            "mode": "discovery_only",
            "collection_window_end": window_end.isoformat() if window_end else None,
        }
    try:
        verify_free_tier(policy, confirmation, key, clock())
        pause = state.data["pause"]
        if pause and pause.get("fingerprint") == fingerprint(key):
            raise PolicyError("authentication_paused_rotate_key")
        if pause:
            state.data["pause"] = None
            state.save()
        if telegram is None:
            raise PolicyError("telegram_secrets_missing_or_invalid")
        if require_youtube_api and not youtube.api_key:
            raise PolicyError("youtube_api_key_missing_processing_held")
        gemini.check_model()
    except KairosError as error:
        reason = error.code
        if isinstance(error, AuthError):
            state.data["pause"] = {"fingerprint": fingerprint(key), "reason": reason}
        state.error(reason, clock())
    count, sent = 0, 0
    processing_errors = []
    queued = list(state.data["videos"].values())
    queued.sort(
        key=lambda v: (
            -(
                v["priority"]
                + (started - timestamp(v["discovered_at"])).total_seconds()
                / (86400 * policy["priority_aging_days"])
            ),
            v["published_at"],
        )
    )
    for video in queued:
        if only_video_id is not None and video["video_id"] != only_video_id:
            continue
        if time.monotonic() - monotonic >= policy["max_run_seconds"] - 300:
            reason = reason or "run_time_budget_reached"
            break
        if video["status"] in ("sent", "delivery_uncertain"):
            continue
        if reason and not (
            reason == "youtube_api_key_missing_processing_held"
            and video["status"] == "ready"
            and telegram is not None
        ):
            continue
        if video.get("retry_at") and timestamp(video["retry_at"]) > clock():
            continue
        if video["status"] == "ready" and telegram is not None:
            try:
                deliver(state, video, telegram)
                sent += 1
            except TelegramError as error:
                video["last_error"] = error.code
                state.error(error.code, clock(), video["video_id"])
                processing_errors.append(error.code)
            continue
        if reason or count >= (
            max_videos if max_videos is not None else policy["max_videos_per_run"]
        ):
            continue
        try:
            seconds = youtube.metadata(video)
            video["duration_seconds"] = seconds
            video["status"] = "analyzing"
            video["attempts"] += 1
            state.save()
            count += 1
            summary = gemini.summarize(video, seconds)
            video["summary"] = summary
            video["channel_name"] = next(
                (
                    row["name"]
                    for row in channels
                    if row["channel_id"] == video["channel_id"]
                ),
                video.get("channel_name", "YouTube"),
            )
            video["parts"] = render(video, summary)
            video["status"] = "ready"
            video["retry_at"] = None
            video.pop("last_error", None)
            state.save()
            deliver(state, video, telegram)
            sent += 1
        except KairosError as error:
            processing_errors.append(error.code)
            if video["status"] == "analyzing":
                video["status"] = "pending"
            video["last_error"] = error.code
            if not isinstance(error, TelegramRateLimit):
                video["retry_at"] = (
                    clock() + timedelta(hours=min(48, 2 ** min(video["attempts"], 5)))
                ).isoformat()
            if isinstance(error, AuthError):
                state.data["pause"] = {
                    "fingerprint": fingerprint(key),
                    "reason": error.code,
                }
                reason = error.code
            elif isinstance(error, QuotaError):
                # Conservative defer: no repeated API probes in this run.
                video["retry_at"] = error.retry_at or gemini.guard.blocked_until()
                reason = error.code
            state.error(error.code, clock(), video["video_id"])
    pending = sum(v["status"] != "sent" for v in state.data["videos"].values())
    uncertain = sum(
        v["status"] == "delivery_uncertain" for v in state.data["videos"].values()
    )
    result = {
        "sent": sent,
        "pending": pending,
        "delivery_uncertain": uncertain,
        "reason": reason,
        "errors": failures,
        "processing_errors": processing_errors,
        "processing_held": reason == "youtube_api_key_missing_processing_held",
    }
    state.data["runs"].append({"at": started.isoformat(), **result})
    state.data["runs"] = state.data["runs"][-120:]
    state.save()
    send_briefing(state, telegram, started, result)
    return result


def briefing_slot(started, namespace="briefing"):
    """Preserve existing slot keys; the morning boundary is now 11:30 KST."""
    slot = boundary(started)
    return f"{namespace}:{slot.date().isoformat()}-{slot.hour:02d}"


def send_briefing(state, telegram, started, result, namespace="briefing"):
    if telegram is None:
        return
    blocked = state.data.get("telegram_blocked_until")
    if blocked and timestamp(blocked) > started:
        return
    slot = briefing_slot(started, namespace)
    previous = state.data["briefings"].get(slot)
    if previous and previous["status"] in ("sent", "sending", "delivery_uncertain"):
        return
    briefing = {"status": "sending", "at": started.isoformat()}
    state.data["briefings"][slot] = briefing
    # Keep recent slots; video delivery tombstones are never pruned.
    for key in sorted(state.data["briefings"])[:-120]:
        del state.data["briefings"][key]
    state.save()
    status = {
        "youtube_api_key_missing_processing_held": "YouTube 조회 키 등록 대기 — 새 영상 감시·저장은 계속하고 요약은 보류",
        "free_quota_exhausted": "무료 한도 대기",
        "free_tier_unverified": "무료 등급 재확인 대기",
        "google_auth_failed": "Gemini 키 인증 오류 — 키 확인 필요",
    }.get(result["reason"], result["reason"])
    text = (
        f"📊 Kairos YouTube 브리핑\n\n이번 실행 발송 {result['sent']}건\n미처리 {result['pending']}건"
        f"\n도착 여부 확인 필요 {result['delivery_uncertain']}건\n조회 오류 {len(result['errors'])}건"
        f"\n분석·발송 오류 {len(result.get('processing_errors', []))}건"
        f"\n상태: {status or ('일부 처리 보류' if result.get('processing_errors') else '완료')}\n무료 한도 대기 영상은 다음 이용 가능 시점에 이월합니다."
    )
    try:
        briefing["message_id"] = telegram.send(text)
        briefing["status"] = "sent"
    except TelegramError as error:
        briefing["status"] = (
            "delivery_uncertain"
            if error.code == "telegram_delivery_uncertain"
            else "pending"
        )
        state.error(error.code, started)
    state.save()
