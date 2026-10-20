"""Fixed publication windows, independent of runner delays and analysis time."""

from datetime import timedelta, timezone

from .errors import PolicyError
from .guard import timestamp

UTC_SLOTS = ((2, 30), (6, 0), (11, 0), (14, 0))


def boundary(now):
    utc = now.astimezone(timezone.utc)
    candidates = [
        utc.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in UTC_SLOTS
    ]
    prior = [value for value in candidates if value <= utc]
    return max(prior) if prior else candidates[-1] - timedelta(days=1)


def initialize_window(state, channels, policy):
    """One-time authorized lookback; preserve deliveries, pending work and quotas."""
    start_value = policy.get("collection_initial_start")
    end_value = policy.get("collection_first_run")
    if not start_value and not end_value:
        return None
    try:
        start, end = timestamp(start_value), timestamp(end_value)
        valid = end - start == timedelta(hours=24) and boundary(end) == end
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise PolicyError("collection_window_invalid")
    revision = start.isoformat() + "/" + end.isoformat()
    if state.data.get("collection", {}).get("revision") != revision:
        for channel in channels:
            if not channel.get("verification", "").startswith("verified"):
                raise PolicyError("channel_unverified")
        for channel in channels:
            state.data["channels"][channel["channel_id"]] = {
                "baseline": start.isoformat(),
                "last_success": None,
            }
        state.data["collection"] = {
            "revision": revision,
            "initial_start": start.isoformat(),
            "first_run": end.isoformat(),
        }
        state.save()
    return end
