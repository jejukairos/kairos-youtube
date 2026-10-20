"""Read-only Telegram checks; never print credentials, chat IDs or raw bodies."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kairos_youtube.__main__ import telegram
from kairos_youtube.errors import KairosError
from kairos_youtube.http import Http


def main():
    client = telegram(Http())
    if client is None:
        print('{"error":"telegram_secrets_missing_or_invalid"}')
        return 2
    for method in ("getMe", "getChat"):
        payload = {} if method == "getMe" else {"chat_id": client.chat_id}
        response = client.http.request(
            "POST",
            f"https://api.telegram.org/bot{client.token}/{method}",
            payload=payload,
        )
        data = response.json()
        result = {
            "method": method,
            "http_status": response.status,
            "ok": data.get("ok") is True,
        }
        value = data.get("parameters", {}).get("retry_after")
        if type(value) is int:
            result["retry_after_seconds"] = value
        value = data.get("error_code")
        if type(value) is int:
            result["telegram_error_code"] = value
        if result["ok"]:
            detail = data.get("result", {})
            result["identity_matches"] = (
                detail.get("is_bot") is True
                if method == "getMe"
                else str(detail.get("id")) == client.chat_id
            )
        print(json.dumps(result), flush=True)
        if not result["ok"] or not result.get("identity_matches"):
            return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KairosError as error:
        print(json.dumps({"error": error.code}))
        raise SystemExit(2) from None
    except Exception:
        print('{"error":"diagnostic_failed_details_redacted"}')
        raise SystemExit(2) from None
