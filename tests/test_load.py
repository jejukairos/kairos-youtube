import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from kairos_youtube.errors import TelegramRateLimit, UncertainDelivery
from kairos_youtube.load import load_report, warn_load
from kairos_youtube.state import State


class LoadWarningTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.s = State(Path(folder.name) / "state.json")
        self.now = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
        self.p = json.loads(
            (Path(__file__).resolve().parents[1] / "config/policy.json").read_text(
                encoding="utf-8"
            )
        )
        self.c = {
            "rpd": 20,
            "tpm": 250000,
            "tokens_per_day": 1000000,
            "youtube_video_seconds_per_day": 28800,
        }
        self.tg = Mock()
        self.tg.send.return_value = 42

    def reserve(self, requests=14, *, at=None, tokens=1000, seconds=100):
        self.s.data["reservations"].append(
            {
                "at": (at or self.now).isoformat(),
                "provider": "gemini",
                "requests": requests,
                "tokens": tokens,
                "seconds": seconds,
            }
        )

    def notify(self):
        return warn_load(self.s, self.tg, self.p, self.c, lambda: self.now)

    def test_below_threshold_sends_nothing_and_never_changes_channel_or_quota(self):
        self.reserve(13)
        self.s.data["channels"]["fixture"] = {"baseline": "preserve"}
        before = copy.deepcopy(self.s.data)
        report = self.notify()
        self.assertEqual(0, report["level"])
        self.tg.send.assert_not_called()
        self.assertEqual(before, self.s.data)

    def test_70_percent_notifies_once_and_90_percent_escalates_once_after_restart(self):
        self.reserve(14)
        before = copy.deepcopy(self.s.data["reservations"])
        self.assertEqual("sent", self.notify()["notification"])
        self.s = State(self.s.path)
        self.s.recover()
        self.assertEqual("already_notified", self.notify()["notification"])
        self.assertEqual(1, self.tg.send.call_count)
        self.assertEqual(before, self.s.data["reservations"])
        self.reserve(4)
        self.assertEqual(2, self.notify()["level"])
        self.notify()
        self.assertEqual(2, self.tg.send.call_count)
        self.assertIn("채널은 자동으로 줄이지 않습니다", self.tg.send.call_args.args[0])

    def test_queue_length_duration_and_text_size_each_warn_with_unknowns_preserved(
        self,
    ):
        self.s.data["videos"] = {str(i): {"status": "pending"} for i in range(10)}
        report = load_report(self.s, self.p, self.c, self.now)
        self.assertIn("pending_videos", report["causes"])
        self.assertEqual(10, report["unknown_duration_videos"])
        self.s.data["videos"] = {
            "long": {"status": "pending", "duration_seconds": 10800}
        }
        self.assertIn(
            "pending_duration", load_report(self.s, self.p, self.c, self.now)["causes"]
        )
        self.s.data["videos"] = {
            "ready": {
                "status": "ready",
                "parts": [{"text": "가" * 12000}],
                "sent_parts": [],
            }
        }
        report = load_report(self.s, self.p, self.c, self.now)
        self.assertEqual(12000, report["summary_characters"])
        self.assertIn("summary_volume", report["causes"])
        self.s.data["videos"]["ready"]["sent_parts"] = [42]
        report = load_report(self.s, self.p, self.c, self.now, delivered_chars=12000)
        self.assertEqual(12000, report["summary_characters"])

    def test_forecast_warns_about_next_request_and_single_large_input_without_ai(self):
        self.c["rpd"] = 2
        self.reserve(1)
        report = load_report(
            self.s,
            self.p,
            self.c,
            self.now,
            upcoming={"duration_seconds": 60, "token_count": {"tokens": 300000}},
        )
        self.assertEqual(2, report["level"])
        self.assertIn("next_request_budget", report["causes"])
        self.assertIn("single_input_too_large", report["causes"])
        self.assertEqual(1, len(self.s.data["reservations"]))

    def test_uncertain_warning_and_interrupted_warning_are_never_auto_resent(self):
        self.reserve()
        self.tg.send.side_effect = UncertainDelivery()
        self.assertEqual("delivery_uncertain", self.notify()["notification"])
        self.s = State(self.s.path)
        self.s.recover()
        self.assertEqual("already_notified", self.notify()["notification"])
        self.assertEqual(1, self.tg.send.call_count)
        outbox = next(iter(self.s.data["load_alerts"].values()))
        outbox["status"] = "sending"
        self.s.recover()
        self.assertEqual("delivery_uncertain", outbox["status"])

    def test_rate_limited_notice_remains_pending_and_respects_shared_wait(self):
        self.reserve()
        self.tg.send.side_effect = TelegramRateLimit(90)
        self.assertEqual("ready", self.notify()["notification"])
        self.assertEqual("waiting", self.notify()["notification"])
        self.assertEqual(1, self.tg.send.call_count)
        self.assertIn("telegram_blocked_until", self.s.data)

    def test_daily_requests_reset_at_pacific_midnight_while_video_time_rolls_24h(self):
        midnight = self.now.replace(hour=7)
        self.reserve(14, at=midnight - timedelta(seconds=1), seconds=100)
        self.reserve(2, at=midnight, seconds=200)
        self.reserve(8, at=midnight - timedelta(hours=25), seconds=999)
        report = load_report(self.s, self.p, self.c, midnight)
        self.assertEqual(2, report["budgets"]["requests"]["used"])
        self.assertEqual(300, report["budgets"]["video_seconds"]["used"])
