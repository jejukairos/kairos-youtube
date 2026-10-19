"""Small bounded REST transport. Never include URLs/bodies in exceptions or logs."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .errors import KairosError


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError):
            raise KairosError("invalid_json_response") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, url, *, headers=None, payload=None, timeout=40):
        data = (
            json.dumps(payload, ensure_ascii=False).encode()
            if payload is not None
            else None
        )
        h = {
            "User-Agent": "KairosYouTube/4.3 (+personal feed monitor)",
            **(headers or {}),
        }
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                body = response.read(8_388_609)
                if len(body) > 8_388_608:
                    raise KairosError("response_too_large")
                return Response(response.status, body, dict(response.headers))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read(4096), dict(e.headers))
        except (OSError, TimeoutError):
            raise KairosError("network_error") from None
