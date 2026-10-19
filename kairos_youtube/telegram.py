import re

from .errors import KairosError, TelegramError, UncertainDelivery


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
    return [
        f"📊 {title}"
        + (f" ({i}/{total})" if total > 1 else "")
        + "\n\n"
        + text
        + "\n\n🔗 "
        + video["url"]
        for i, text in enumerate(messages, 1)
    ]


class Telegram:
    def __init__(self, http, token, chat_id):
        if not re.fullmatch(r"-?\d+", chat_id or "") or not token:
            raise TelegramError("telegram_secrets_missing_or_invalid")
        self.http, self.token, self.chat_id = http, token, chat_id

    def send(self, text):
        if units(text) > 4096:
            raise TelegramError("telegram_message_too_long")
        try:
            response = self.http.request(
                "POST",
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                payload={
                    "chat_id": self.chat_id,
                    "text": text,
                    "link_preview_options": {"is_disabled": True},
                },
                timeout=40,
            )
        except KairosError:
            raise UncertainDelivery() from None
        if response.status >= 500:
            raise UncertainDelivery()
        if response.status != 200:
            raise TelegramError()
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
    for index, part in enumerate(video["parts"]):
        if index < len(video["sent_parts"]):
            continue
        video["status"] = "sending"
        state.save()  # Persist before Telegram can accept the message.
        try:
            mid = telegram.send(part)
        except UncertainDelivery:
            video["status"] = "delivery_uncertain"
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
    state.save()
