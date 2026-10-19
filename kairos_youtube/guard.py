import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .errors import PolicyError, QuotaError

UTC = timezone.utc
PACIFIC = ZoneInfo("America/Los_Angeles")
# One-way digest of a short suffix of a credential exposed in this session.
# Neither the original credential nor its literal suffix is stored in source.
REVOKED_KEY_SUFFIX_SHA256 = {
    "e4ea7e3088d56b679d2c63a20a06688fe281156d2f9dbc2f40b05c4f0fb60b71"
}


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(UTC)


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def check_key_safety(key):
    if not key:
        raise PolicyError("gemini_key_missing")
    if len(key) < 30 or any(char.isspace() for char in key):
        raise PolicyError("gemini_key_format_invalid")
    if fingerprint(key[-12:]) in REVOKED_KEY_SUFFIX_SHA256:
        raise PolicyError("exposed_gemini_key_must_be_rotated")


def validate_policy(policy):
    if (
        policy.get("FREE_ONLY") is not True
        or policy.get("MAX_SPEND_USD") != 0
        or policy.get("ALLOW_PAID_FALLBACK") is not False
        or policy.get("AI_PROVIDER") != "gemini"
    ):
        raise PolicyError("zero_cost_policy_violated")
    for key, expected in (
        ("FREE_ONLY", "true"),
        ("MAX_SPEND_USD", "0"),
        ("ALLOW_PAID_FALLBACK", "false"),
        ("AI_PROVIDER", "gemini"),
    ):
        if os.environ.get(key, expected).lower() != expected:
            raise PolicyError("zero_cost_environment_violated")
    # Explicit reviewed model allowlist; no mutable aliases or paid fallbacks.
    if policy.get("model") != "gemini-3.8-flash":
        raise PolicyError("model_requires_free_pricing_review")


def verify_free_tier(policy, confirmation, key, now):
    validate_policy(policy)
    check_key_safety(key)
    try:
        checked = timestamp(policy["pricing_checked_on"] + "T00:00:00+00:00")
        start, end = (
            timestamp(confirmation["confirmed_at"]),
            timestamp(confirmation["expires_at"]),
        )
        valid = (
            timedelta(0)
            <= now - checked
            <= timedelta(days=policy["pricing_review_days"])
            and start <= now < end <= start + timedelta(days=7)
            and confirmation["tier"] == "free"
            and confirmation["billing_enabled"] is False
            and confirmation["key_rotated"] is True
            and confirmation["project_id"]
            and confirmation["model"] == policy["model"]
            and confirmation["key_sha256"] == fingerprint(key)
            and all(
                type(confirmation[k]) is int and confirmation[k] > 0
                for k in (
                    "rpm",
                    "tpm",
                    "rpd",
                    "tokens_per_day",
                    "youtube_video_seconds_per_day",
                )
            )
        )
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise PolicyError()


class Guard:
    def __init__(self, state, policy, confirmation, clock):
        self.state, self.policy, self.confirmation, self.clock = (
            state,
            policy,
            confirmation,
            clock,
        )

    def reserve(
        self, *, provider, requests=1, tokens=0, seconds=0, units=0, video_id=None
    ):
        now = self.clock()
        rows = self.state.data["reservations"]
        day = now.astimezone(PACIFIC).date()
        today = [
            r
            for r in rows
            if r["provider"] == provider
            and timestamp(r["at"]).astimezone(PACIFIC).date() == day
        ]
        minute = [
            r
            for r in rows
            if r["provider"] == provider
            and timestamp(r["at"]) > now - timedelta(seconds=61)
        ]
        if provider == "gemini":
            c = self.confirmation
            rolling = [
                r
                for r in rows
                if r["provider"] == provider
                and timestamp(r["at"]) > now - timedelta(hours=24)
            ]
            caps = [
                (sum(r["requests"] for r in today) + requests, c["rpd"]),
                (sum(r["requests"] for r in minute) + requests, c["rpm"]),
                (sum(r["tokens"] for r in minute) + tokens, c["tpm"]),
                (sum(r["tokens"] for r in today) + tokens, c["tokens_per_day"]),
                (
                    sum(r["seconds"] for r in rolling) + seconds,
                    min(
                        28800,
                        self.policy["free_video_seconds_per_day"],
                        c["youtube_video_seconds_per_day"],
                    ),
                ),
            ]
            exceeded = [i for i, (used, cap) in enumerate(caps) if used > cap]
            if exceeded:
                retry = self.blocked_until()
                if all(i in (1, 2) for i in exceeded):
                    # Single requests larger than TPM cannot fit later either.
                    retry = (
                        (now + timedelta(seconds=62)).isoformat()
                        if tokens <= c["tpm"]
                        else self.blocked_until()
                    )
                raise QuotaError(retry_at=retry)
        elif provider == "youtube":
            if (
                sum(r["units"] for r in today) + units
                > self.policy["youtube_units_per_day"]
            ):
                raise QuotaError("youtube_quota_exhausted")
        rows[:] = [r for r in rows if timestamp(r["at"]) > now - timedelta(days=2)]
        rows.append(
            {
                "at": now.isoformat(),
                "provider": provider,
                "requests": requests,
                "tokens": tokens,
                "seconds": seconds,
                "units": units,
                "video_id": video_id,
            }
        )
        self.state.save()

    def blocked_until(self):
        now = self.clock()
        local = now.astimezone(PACIFIC)
        reset = datetime.combine(
            local.date() + timedelta(days=1), datetime.min.time(), PACIFIC
        )
        # Video reset is undocumented; rolling 24h is deliberately stricter.
        return max(reset.astimezone(UTC), now + timedelta(hours=24)).isoformat()


def load_confirmation():
    # Never print this variable. It contains a fingerprint, not the key.
    try:
        return json.loads(os.environ.get("FREE_TIER_CONFIRMATION", "{}"))
    except ValueError:
        return {}
