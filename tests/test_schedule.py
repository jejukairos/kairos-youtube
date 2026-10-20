import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from kairos_youtube.errors import DiscoveryError, PolicyError
from kairos_youtube.runner import briefing_slot, run
from kairos_youtube.schedule import boundary, initialize_window
from kairos_youtube.state import State
from kairos_youtube.youtube import discover

UTC = timezone.utc
START = datetime(2026, 10, 19, 2, 30, tzinfo=UTC)
FIRST = START + timedelta(days=1)
CHANNEL = {
    "channel_id": "UC" + "a" * 22,
    "name": "fixture",
    "verification": "verified_fixture",
    "priority": 50,
}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = State(Path(folder.name) / "state.json")
        self.policy = json.loads(
            (Path(__file__).resolve().parents[1] / "config/policy.json").read_text(
                encoding="utf-8"
            )
        )

    def entry(self, number, published):
        vid = f"{number:011d}"
        return {
            "video_id": vid,
            "channel_id": CHANNEL["channel_id"],
            "published_at": published.isoformat(),
            "title": "fixture",
            "url": "https://www.youtube.com/watch?v=" + vid,
        }

    def youtube(self, entries):
        youtube = Mock()
        youtube.api_key = ""
        youtube.feed.return_value = sorted(
            entries, key=lambda row: row["published_at"], reverse=True
        )
        return youtube

    def test_morning_boundary_is_1130_kst_and_delays_keep_same_window(self):
        self.assertEqual(
            FIRST - timedelta(hours=12, minutes=30),
            boundary(FIRST - timedelta(seconds=1)),
        )
        self.assertEqual(FIRST, boundary(FIRST))
        self.assertEqual(FIRST, boundary(FIRST + timedelta(minutes=40)))
        self.assertNotEqual(
            briefing_slot(FIRST - timedelta(seconds=1)), briefing_slot(FIRST)
        )
        kst = timezone(timedelta(hours=9))
        self.assertEqual(boundary(FIRST), boundary(FIRST.astimezone(kst)))
        self.assertEqual(
            FIRST.replace(hour=14, minute=0) - timedelta(days=1),
            boundary(FIRST.replace(hour=0, minute=1)),
        )

    def test_one_time_rewind_preserves_queue_delivery_and_quota(self):
        self.state.data["channels"][CHANNEL["channel_id"]] = {
            "baseline": (FIRST - timedelta(hours=2)).isoformat(),
            "last_success": (FIRST - timedelta(hours=1)).isoformat(),
        }
        self.state.data["videos"] = {
            "sent": {"status": "sent", "sent_parts": [5]},
            "pending": {"status": "pending", "attempts": 2},
        }
        self.state.data["reservations"] = [{"fixture": "preserve"}]
        original = copy.deepcopy(self.state.data)
        initialize_window(self.state, [CHANNEL], self.policy)
        self.assertEqual(original["videos"], self.state.data["videos"])
        self.assertEqual(original["reservations"], self.state.data["reservations"])
        cursor = self.state.data["channels"][CHANNEL["channel_id"]]
        self.assertEqual(START.isoformat(), cursor["baseline"])
        self.assertIsNone(cursor["last_success"])
        cursor["last_success"] = FIRST.isoformat()
        self.state.save()
        restored = State(self.state.path)
        initialize_window(restored, [CHANNEL], self.policy)
        self.assertEqual(
            FIRST.isoformat(),
            restored.data["channels"][CHANNEL["channel_id"]]["last_success"],
        )

    def test_before_first_run_only_saves_baseline_without_external_calls(self):
        youtube, gemini, telegram = Mock(), Mock(), Mock()
        result = run(
            self.state,
            [CHANNEL],
            youtube,
            gemini,
            telegram,
            self.policy,
            {},
            "",
            lambda: FIRST - timedelta(hours=1),
            discovery_only=True,
        )
        self.assertEqual("discovery_only", result["mode"])
        self.assertEqual(START.isoformat(), result["collection_initial_start"])
        self.assertEqual([], youtube.mock_calls)
        self.assertEqual([], gemini.mock_calls)
        self.assertEqual([], telegram.mock_calls)
        self.assertIsNone(
            self.state.data["channels"][CHANNEL["channel_id"]]["last_success"]
        )

    def test_first_24_hours_then_adjacent_window_with_no_duplicates(self):
        initialize_window(self.state, [CHANNEL], self.policy)
        entries = [
            self.entry(1, START - timedelta(seconds=1)),
            self.entry(2, START),
            self.entry(3, FIRST - timedelta(seconds=1)),
            self.entry(4, FIRST),
            self.entry(5, FIRST + timedelta(minutes=1)),
        ]
        youtube = self.youtube(entries)
        discover(
            self.state,
            youtube,
            [CHANNEL],
            lambda: FIRST + timedelta(minutes=10),
            window_end=FIRST,
        )
        self.assertEqual({f"{i:011d}" for i in (2, 3)}, set(self.state.data["videos"]))
        self.state.data["videos"][f"{2:011d}"]["status"] = "sent"
        afternoon = FIRST.replace(hour=6, minute=0)
        youtube.feed.return_value += [self.entry(6, afternoon)]
        youtube.feed.return_value.sort(
            key=lambda row: row["published_at"], reverse=True
        )
        discover(
            self.state, youtube, [CHANNEL], lambda: afternoon, window_end=afternoon
        )
        self.assertEqual(
            {f"{i:011d}" for i in (2, 3, 4, 5)}, set(self.state.data["videos"])
        )
        self.assertEqual("sent", self.state.data["videos"][f"{2:011d}"]["status"])
        self.assertEqual(
            afternoon.isoformat(),
            self.state.data["channels"][CHANNEL["channel_id"]]["last_success"],
        )

    def test_failed_window_and_late_feed_entries_are_recovered(self):
        initialize_window(self.state, [CHANNEL], self.policy)
        youtube = self.youtube([])
        youtube.feed.side_effect = DiscoveryError()
        discover(self.state, youtube, [CHANNEL], lambda: FIRST, window_end=FIRST)
        cursor = self.state.data["channels"][CHANNEL["channel_id"]]
        self.assertIsNone(cursor["last_success"])
        youtube.feed.side_effect = None
        youtube.feed.return_value = [
            self.entry(1, START - timedelta(seconds=1)),
            self.entry(2, FIRST - timedelta(minutes=1)),
        ]
        youtube.feed.return_value.sort(
            key=lambda row: row["published_at"], reverse=True
        )
        afternoon = FIRST.replace(hour=6, minute=0)
        discover(
            self.state, youtube, [CHANNEL], lambda: afternoon, window_end=afternoon
        )
        self.assertIn(f"{2:011d}", self.state.data["videos"])
        youtube.feed.return_value += [self.entry(3, FIRST - timedelta(minutes=5))]
        youtube.feed.return_value.sort(
            key=lambda row: row["published_at"], reverse=True
        )
        evening = FIRST.replace(hour=11, minute=0)
        discover(self.state, youtube, [CHANNEL], lambda: evening, window_end=evening)
        self.assertTrue(self.state.data["videos"][f"{3:011d}"]["late_discovery"])

    def test_invalid_initial_window_is_rejected(self):
        with self.assertRaises(PolicyError):
            initialize_window(
                self.state,
                [CHANNEL],
                {
                    **self.policy,
                    "collection_first_run": (FIRST + timedelta(hours=1)).isoformat(),
                },
            )
