"""Early load warnings from existing state; never call an AI to count usage."""

from datetime import timedelta

from .errors import TelegramError
from .guard import PACIFIC, timestamp
from .telegram import deliver


def text_size(video):
    return sum(
        len(part.get("text", "") if isinstance(part, dict) else part)
        for part in video.get("parts", [])[len(video.get("sent_parts", [])) :]
    )


def load_report(state, policy, confirmation, now, *, delivered_chars=0, upcoming=None):
    settings = policy.get("load_warning", {})
    warn = settings.get("usage_percent", 70)
    critical = settings.get("critical_percent", 90)
    day = now.astimezone(PACIFIC).date()
    rows = [r for r in state.data["reservations"] if r["provider"] == "gemini"]
    today = [r for r in rows if timestamp(r["at"]).astimezone(PACIFIC).date() == day]
    rolling = [r for r in rows if timestamp(r["at"]) > now - timedelta(hours=24)]

    def cap(name):
        value = confirmation.get(name)
        return value if type(value) is int and value > 0 else None

    videos = list(state.data["videos"].values())
    pending = [v for v in videos if v["status"] in ("pending", "analyzing")]
    seconds = [v.get("duration_seconds") for v in pending]
    known = [value for value in seconds if type(value) is int and value > 0]
    budgets = {
        "requests": {"used": sum(r["requests"] for r in today), "limit": cap("rpd")},
        "tokens": {
            "used": sum(r["tokens"] for r in today),
            "limit": cap("tokens_per_day"),
        },
        "video_seconds": {
            "used": sum(r["seconds"] for r in rolling),
            "limit": min(
                policy["free_video_seconds_per_day"],
                cap("youtube_video_seconds_per_day") or 28800,
                28800,
            ),
        },
    }
    causes, level = [], 0
    for name, budget in budgets.items():
        limit = budget["limit"]
        budget["percent"] = (
            budget["used"] * 100 // limit if type(limit) is int and limit > 0 else None
        )
        if budget["percent"] is not None and budget["percent"] >= warn:
            causes.append(name)
            level = max(level, 2 if budget["percent"] >= critical else 1)
    chars = delivered_chars + sum(
        text_size(v) for v in videos if v["status"] == "ready"
    )
    for value, threshold, name in (
        (len(pending), settings.get("pending_videos", 10), "pending_videos"),
        (sum(known), settings.get("pending_seconds", 10800), "pending_duration"),
        (chars, settings.get("summary_characters", 12000), "summary_volume"),
    ):
        if value >= threshold:
            causes.append(name)
            level = max(level, 2 if value >= threshold * 2 else 1)
    if upcoming:
        # Conservative forecast, not authorization: count + generation may
        # process the full input twice. Guard still checks exact API requests.
        duration = upcoming.get("duration_seconds", 0)
        if (
            budgets["requests"]["limit"]
            and budgets["requests"]["used"] + 2 > budgets["requests"]["limit"]
        ):
            causes.append("next_request_budget")
            level = 2
        if (
            budgets["video_seconds"]["used"] + 2 * duration
            > budgets["video_seconds"]["limit"]
        ):
            causes.append("next_video_budget")
            level = 2
        counted = upcoming.get("token_count", {}).get("tokens", 0)
        if (
            counted
            and budgets["tokens"]["limit"]
            and budgets["tokens"]["used"] + counted + policy["max_output_tokens"]
            > budgets["tokens"]["limit"]
        ):
            causes.append("next_token_budget")
            level = 2
        if (
            counted
            and cap("tpm")
            and counted + policy["max_output_tokens"] > cap("tpm")
        ):
            causes.append("single_input_too_large")
            level = 2
    return {
        "day": day.isoformat(),
        "level": level,
        "causes": causes,
        "pending_videos": len(pending),
        "known_pending_seconds": sum(known),
        "unknown_duration_videos": len(pending) - len(known),
        "summary_characters": chars,
        "budgets": budgets,
    }


def warning_text(report):
    budgets = report["budgets"]
    requests, tokens, video = (
        budgets[k] for k in ("requests", "tokens", "video_seconds")
    )
    labels = {
        "requests": "일일 요청 예산",
        "tokens": "자체 일일 토큰 예산",
        "video_seconds": "영상 처리 시간 예산",
        "pending_videos": "대기 영상 수",
        "pending_duration": "대기 영상 길이",
        "summary_volume": "이번 요약 글자 수",
        "next_request_budget": "다음 영상의 요청 예산 부족 예상",
        "next_video_budget": "다음 영상의 시간 예산 부족 예상",
        "next_token_budget": "다음 영상의 자체 토큰 예산 부족 예상",
        "single_input_too_large": "영상 하나의 입력량이 분당 한도 초과",
    }

    def fraction(value):
        return (
            f"{value['used']:,}/{value['limit']:,} ({value['percent']}%)"
            if type(value["limit"]) is int
            else f"{value['used']:,}/한도 미확인"
        )

    return (
        "⚠️ 영상 처리량 사전 안내\n\n"
        + "확인 항목: "
        + ", ".join(labels[key] for key in report["causes"])
        + f"\n분석 대기 {report['pending_videos']}개 · 확인된 영상 길이 {report['known_pending_seconds'] // 60}분"
        + f"\n길이 미확인 {report['unknown_duration_videos']}개 · 이번 요약 {report['summary_characters']:,}자"
        + f"\nGemini 요청 예약: {fraction(requests)}회"
        + f"\n자체 일일 토큰 예약: {fraction(tokens)}"
        + f"\n영상 시간 예약(최근 24시간): {video['used'] // 60}/{video['limit'] // 60}분"
        + "\n\n이 프로그램의 예약 장부이며 테스트도 포함합니다."
        + "\n채널 조정은 Codex 대화에 제외할 채널명을 보내주세요. 채널은 자동으로 줄이지 않습니다."
        + "\n무료 한도가 부족하면 남은 영상을 저장해 이월하며 유료로 전환하지 않습니다."
    )


def warn_load(state, telegram, policy, confirmation, clock, **kwargs):
    report = load_report(state, policy, confirmation, clock(), **kwargs)
    if not report["level"] or telegram is None:
        return report
    alerts = state.data.setdefault("load_alerts", {})
    # At most two accepted/uncertain notices per quota day. An escalated notice
    # suppresses lower-severity notices, including across process restarts.
    for severity in range(report["level"], 3):
        previous = alerts.get(f"{report['day']}:{severity}")
        if previous and previous["status"] in ("sent", "sending", "delivery_uncertain"):
            report["notification"] = "already_notified"
            return report
    slot = f"{report['day']}:{report['level']}"
    outbox = alerts.setdefault(slot, {"status": "ready", "parts": [], "sent_parts": []})
    if not outbox["sent_parts"]:
        outbox["parts"] = [warning_text(report)]
    state.save()
    blocked = state.data.get("telegram_blocked_until")
    if (blocked and timestamp(blocked) > clock()) or (
        outbox.get("retry_at") and timestamp(outbox["retry_at"]) > clock()
    ):
        report["notification"] = "waiting"
        return report
    try:
        deliver(state, outbox, telegram)
    except TelegramError as error:
        state.error(error.code, clock())
    report["notification"] = outbox["status"]
    return report
