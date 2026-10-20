import re
from datetime import datetime, timedelta, timezone

from .errors import (
    KairosError,
    PolicyError,
    TelegramError,
    TelegramRateLimit,
    UncertainDelivery,
)
from .guard import timestamp


def units(text):
    return len(text.encode("utf-16-le")) // 2


def split_text(text, limit=3400):
    chunks, current = [], ""
    for char in text:
        if units(current + char) > limit:
            chunks.append(current)
            current = ""
        current += char
    if current:
        chunks.append(current)
    return chunks


def render(video, summary):
    title = " ".join(video["title"].split())[:250]
    blocks = ["▶️ " + b["text"].strip() for b in summary["bullets"]]
    messages, body = [], ""
    for block in blocks:
        for piece in split_text(block):
            candidate = body + ("\n\n" if body else "") + piece
            if units(candidate) > 3400:
                messages.append(body)
                body = piece
            else:
                body = candidate
    if body:
        messages.append(body)
    total = len(messages)
    channel = " ".join(video.get("channel_name", "YouTube").split())[:150]
    return [
        {
            "text": f"📺 {channel}\n📊 {title}\n\n{video['url']}",
            "preview_url": video["url"],
        }
    ] + [
        {
            "text": f"📊 {title}"
            + (f" ({i}/{total})" if total > 1 else "")
            + "\n\n"
            + text
        }
        for i, text in enumerate(messages, 1)
    ]


class Telegram:
    def __init__(self, http, token, chat_id):
        if not re.fullmatch(r"-?\d+", chat_id or "") or not token:
            raise TelegramError("telegram_secrets_missing_or_invalid")
        self.http, self.token, self.chat_id = http, token, chat_id

    def send(self, text, *, preview_url=None):
        if units(text) > 4096:
            raise TelegramError("telegram_message_too_long")
        preview = {"is_disabled": True}
        if preview_url is not None:
            if (
                not re.fullmatch(
                    r"https://www\.youtube\.com/watch\?v=[\w-]{11}", preview_url
                )
                or preview_url not in text
            ):
                raise TelegramError("telegram_preview_url_invalid")
            preview = {
                "is_disabled": False,
                "url": preview_url,
                "prefer_large_media": True,
                "show_above_text": True,
            }
        try:
            response = self.http.request(
                "POST",
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                payload={
                    "chat_id": self.chat_id,
                    "text": text,
                    "link_preview_options": preview,
                },
                timeout=40,
            )
        except KairosError:
            raise UncertainDelivery() from None
        if response.status >= 500:
            raise UncertainDelivery()
        if response.status == 429:
            retry_after = 3600
            try:
                value = response.json().get("parameters", {}).get("retry_after")
                if type(value) is int and value > 0:
                    retry_after = value
            except (KairosError, AttributeError, TypeError):
                pass
            raise TelegramRateLimit(retry_after)
        if response.status != 200:
            # Fixed categories only: never expose raw provider descriptions,
            # request URLs, tokens or chat identifiers in failure logs.
            reason = "request_rejected"
            try:
                description = response.json().get("description", "").lower()
                for needle, label in (
                    ("message is too long", "message_too_long"),
                    ("message text is empty", "empty_text"),
                    ("chat not found", "chat_not_found"),
                    ("bot was blocked", "bot_blocked"),
                    ("too many requests", "rate_limited"),
                    ("unauthorized", "token_invalid"),
                    ("can't parse entities", "invalid_entities"),
                    ("url", "url_rejected"),
                    ("preview", "preview_rejected"),
                ):
                    if needle in description:
                        reason = label
                        break
            except (ValueError, AttributeError, TypeError, KairosError):
                pass
            raise TelegramError(f"telegram_http_{response.status}_{reason}")
        try:
            data = response.json()
            if data.get("ok") is not True:
                raise TelegramError()
            result = data["result"]
            if (
                str(result["chat"]["id"]) != self.chat_id
                or type(result["message_id"]) is not int
            ):
                raise UncertainDelivery()
            return result["message_id"]
        except (KeyError, ValueError, KairosError):
            # An invalid 200 response might still mean Telegram accepted it.
            raise UncertainDelivery() from None


def deliver(state, video, telegram):
    blocked = state.data.get("telegram_blocked_until")
    if blocked and timestamp(blocked) > datetime.now(timezone.utc):
        raise TelegramRateLimit(
            max(
                1,
                int((timestamp(blocked) - datetime.now(timezone.utc)).total_seconds())
                + 1,
            )
        )
    for index, part in enumerate(video["parts"]):
        if index < len(video["sent_parts"]):
            continue
        video["status"] = "sending"
        state.save()  # Persist before Telegram can accept the message.
        try:
            # Existing persisted string parts keep their original delivery format.
            mid = (
                telegram.send(part["text"], preview_url=part.get("preview_url"))
                if isinstance(part, dict)
                else telegram.send(part)
            )
        except UncertainDelivery:
            video["status"] = "delivery_uncertain"
            state.save()
            raise
        except TelegramRateLimit as error:
            video["status"] = "ready"
            video["retry_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=error.retry_after + 1)
            ).isoformat()
            video["telegram_retry_at"] = video["retry_at"]
            state.data["telegram_blocked_until"] = video["retry_at"]
            state.save()
            raise
        except TelegramError:
            video["status"] = "ready"
            state.save()
            raise
        video["sent_parts"].append(mid)
        video["status"] = "ready"
        state.save()
    video["status"] = "sent"
    video["retry_at"] = None
    video.pop("telegram_retry_at", None)
    video.pop("last_error", None)
    state.save()


def resume_saved(
    state, video_id, telegram, clock, *, review_legacy_wait=False, plain_link=False
):
    """Delivery only: no Gemini credential, metadata lookup or analysis call."""
    state.recover()
    video = state.data["videos"].get(video_id)
    if not video or video["status"] not in ("ready", "sent"):
        raise PolicyError("saved_delivery_not_ready_or_uncertain")
    if video["status"] == "sent":
        return {
            "delivery": "sent",
            "video_id": video_id,
            "message_count": len(video["sent_parts"]),
        }
    if telegram is None:
        raise PolicyError("telegram_secrets_missing_or_invalid")
    if not video.get("summary") or not video.get("parts"):
        raise PolicyError("saved_delivery_missing_summary")
    retry = video.get("retry_at")
    if retry and timestamp(retry) > clock():
        errors = [e for e in state.data["errors"] if e.get("video_id") == video_id]
        legacy = (
            review_legacy_wait
            and video.get("last_error") == "telegram_send_failed"
            and not video.get("telegram_retry_at")
            and not video.get("legacy_delivery_wait_reviewed_at")
            and errors
            and errors[-1]["code"] == "telegram_http_429_rate_limited"
            and clock() - timestamp(errors[-1]["at"]) >= timedelta(minutes=5)
        )
        if not legacy:
            return {"delivery": "waiting", "retry_at": retry, "video_id": video_id}
        # The old 4-hour exponential wait was generated by our code, not a
        # Telegram retry_after. One owner-requested review is recorded; future
        # provider deadlines cannot be cleared through this flag.
        video["legacy_delivery_wait_reviewed_at"] = clock().isoformat()
        video["retry_at"] = None
        state.save()
    if plain_link:
        # Only confirmed-unsent parts may change. Keep the URL itself and all
        # summary text; suppress fetching a preview for this reviewed delivery.
        for part in video["parts"][len(video["sent_parts"]) :]:
            if isinstance(part, dict):
                part.pop("preview_url", None)
        video["plain_link_delivery"] = True
        state.save()
    try:
        deliver(state, video, telegram)
    except TelegramRateLimit:
        video["last_error"] = "telegram_rate_limited"
        state.error("telegram_rate_limited", clock(), video_id)
        return {
            "delivery": "waiting",
            "video_id": video_id,
            "retry_at": video.get("retry_at")
            or state.data.get("telegram_blocked_until"),
            "message_count": len(video["sent_parts"]),
        }
    return {
        "delivery": "sent",
        "video_id": video_id,
        "message_count": len(video["sent_parts"]),
    }


def send_preview_test(state, video_id, channels, telegram):
    """Reuse a saved summary once; never analyze again or reset sent videos."""
    from .errors import PolicyError
    from .gemini import validate_summary

    video = state.data["videos"].get(video_id)
    names = {row["channel_id"]: row["name"] for row in channels}
    if not video or video["status"] != "sent" or video["channel_id"] not in names:
        raise PolicyError("preview_test_requires_sent_registered_video")
    validate_summary(video["summary"], video, video["duration_seconds"])
    state.recover()
    demos = state.data.setdefault("presentations", {})
    slot = "youtube-preview-v1:" + video_id
    if slot not in demos:
        source = {**video, "channel_name": names[video["channel_id"]]}
        demos[slot] = {
            "status": "ready",
            "parts": render(source, video["summary"]),
            "sent_parts": [],
        }
        state.save()
    outbox = demos[slot]
    if outbox["status"] == "delivery_uncertain":
        raise PolicyError("preview_test_delivery_uncertain_check_chat")
    if telegram is None:
        raise PolicyError("telegram_secrets_missing_or_invalid")
    if outbox["status"] != "sent":
        deliver(state, outbox, telegram)
    return {
        "preview_test": outbox["status"],
        "message_count": len(outbox["sent_parts"]),
        "video_id": video_id,
    }
