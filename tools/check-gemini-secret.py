"""Check a registered key without displaying it or calling any Google API."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kairos_youtube.errors import KairosError
from kairos_youtube.guard import check_key_safety, fingerprint

try:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    check_key_safety(key)
    # A SHA-256 digest does not grant API access. Never output key characters.
    print(
        "GEMINI_KEY_CHECK="
        + json.dumps(
            {"status": "registered_key_safety_checked", "sha256": fingerprint(key)}
        )
    )
except KairosError as error:
    print(json.dumps({"error": error.code}), file=sys.stderr)
    raise SystemExit(2) from None
except Exception:
    print('{"error":"gemini_key_check_failed_details_redacted"}', file=sys.stderr)
    raise SystemExit(2) from None
