import time
from datetime import timedelta

from .errors import AuthError, KairosError, PolicyError, QuotaError, TelegramError
from .guard import fingerprint, timestamp, verify_free_tier
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
):
    started, monotonic = clock(), time.monotonic()
    state.recover()
    failures = discover(state, youtube, channels, clock)
    reason = None
    if discovery_only:
        return {
            "discovered": len(state.data["videos"]),
            "errors": failures,
            "mode": "discovery_only",
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
        if reason:
            continue
        if video["status"] == "ready" and telegram is not None:
            try:
                deliver(state, video, telegram)
                sent += 1
            except TelegramError as error:
                state.error(error.code, clock(), video["video_id"])
                processing_errors.append(error.code)
            continue
        if reason or count >= (
            max_videos if max_videos is not None else policy["max_videos_per_run"]
        ):
            continue
        if video.get("retry_at") and timestamp(video["retry_at"]) > clock():
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
            video["parts"] = render(video, summary)
            video["status"] = "ready"
            video["retry_at"] = None
            state.save()
            deliver(state, video, telegram)
            sent += 1
        except KairosError as error:
            processing_errors.append(error.code)
            if video["status"] == "analyzing":
                video["status"] = "pending"
            video["last_error"] = error.code
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
    }
    state.data["runs"].append({"at": started.isoformat(), **result})
    state.data["runs"] = state.data["runs"][-120:]
    state.save()
    send_briefing(state, telegram, started, result)
    return result


def send_briefing(state, telegram, started, result, namespace="briefing"):
    if telegram is None:
        return
    # Twice-daily UTC slots correspond to 12:30 / 21:30 KST.
    slot = (
        namespace
        + ":"
        + started.date().isoformat()
        + ("-03" if started.hour < 12 else "-12")
    )
    previous = state.data["briefings"].get(slot)
    if previous and previous["status"] in ("sent", "sending", "delivery_uncertain"):
        return
    briefing = {"status": "sending", "at": started.isoformat()}
    state.data["briefings"][slot] = briefing
    # Keep recent slots; video delivery tombstones are never pruned.
    for key in sorted(state.data["briefings"])[:-120]:
        del state.data["briefings"][key]
    state.save()
    text = (
        f"📊 Kairos YouTube 브리핑\n\n이번 실행 발송 {result['sent']}건\n미처리 {result['pending']}건"
        f"\n도착 여부 확인 필요 {result['delivery_uncertain']}건\n조회 오류 {len(result['errors'])}건"
        f"\n분석·발송 오류 {len(result.get('processing_errors', []))}건"
        f"\n상태: {result['reason'] or ('일부 처리 보류' if result.get('processing_errors') else '완료')}\n무료 한도 대기 영상은 다음 이용 가능 시점에 이월합니다."
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
