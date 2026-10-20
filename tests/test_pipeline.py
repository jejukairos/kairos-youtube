import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from kairos_youtube.errors import (
    AuthError,
    DiscoveryError,
    GeminiError,
    KairosError,
    PolicyError,
    QuotaError,
    TelegramError,
    TelegramRateLimit,
    UncertainDelivery,
    VideoError,
)
from kairos_youtube.gemini import (
    Gemini,
    canonical_evidence_times,
    validate_summary,
    video_timestamp_seconds,
)
from kairos_youtube.guard import Guard, fingerprint, timestamp, verify_free_tier
from kairos_youtube.http import Http, Response
from kairos_youtube.runner import briefing_slot, run, send_briefing
from kairos_youtube.state import State
from kairos_youtube.telegram import Telegram, deliver, render, send_preview_test, units
from kairos_youtube.youtube import YouTube, discover

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc)
KEY = "fixture-only-not-a-real-credential"
CID = "UCaWi2foADm_lKAKnmeQwLSA"
VID = "abcdefghijk"


def policy():
    return json.loads((ROOT / "config/policy.json").read_text(encoding="utf-8"))


def confirmation():
    return {
        "project_id": "fixture-project",
        "tier": "free",
        "billing_enabled": False,
        "key_rotated": True,
        "dedicated_project": True,
        "confirmed_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=6)).isoformat(),
        "model": policy()["model"],
        "key_sha256": fingerprint(KEY),
        "rpm": 10,
        "tpm": 1_000_000,
        "rpd": 20,
        "tokens_per_day": 2_000_000,
        "youtube_video_seconds_per_day": 28800,
    }


def video():
    return {
        "video_id": VID,
        "channel_id": CID,
        "title": "테스트 영상 (실제 분석 아님)",
        "url": "https://www.youtube.com/watch?v=" + VID,
        "published_at": NOW.isoformat(),
        "discovered_at": NOW.isoformat(),
        "priority": 50,
        "status": "pending",
        "attempts": 0,
        "retry_at": None,
        "parts": [],
        "sent_parts": [],
    }


def summary():
    return {
        "video_id": VID,
        "content_access": "full_video",
        "coverage_complete": True,
        "observed_duration_seconds": 100,
        "bullets": [
            {
                "text": f"모의 검증용 문단 {i}: 실제 영상에서 나온 주장이 아닙니다.",
                "evidence": [
                    {
                        "start_seconds": i * 20,
                        "end_seconds": i * 20 + 18,
                        "detail": f"가상 근거 {i}",
                    }
                ],
            }
            for i in range(5)
        ],
    }


def response(data, status=200):
    return Response(status, json.dumps(data).encode(), {})


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = State(Path(self.temp.name) / "state.json")
        self.p, self.c = policy(), confirmation()
        # Generic pipeline fixtures predate the owner's dated activation.
        # Publication-window activation is exercised separately below.
        self.p.pop("collection_initial_start", None)
        self.p.pop("collection_first_run", None)
        self.clock = Mock(return_value=NOW)
        self.g = Guard(self.s, self.p, self.c, self.clock)


class SecurityTests(Base):
    def test_registered_exposed_key_is_blocked_before_provider_access(self):
        with patch(
            "kairos_youtube.guard.REVOKED_KEY_SUFFIX_SHA256", {fingerprint(KEY[-12:])}
        ):
            with self.assertRaises(PolicyError) as caught:
                verify_free_tier(self.p, self.c, KEY, NOW)
            self.assertEqual(
                "exposed_gemini_key_must_be_rotated", caught.exception.code
            )

    def test_missing_key_never_verifies(self):
        with self.assertRaises(PolicyError):
            verify_free_tier(self.p, self.c, "", NOW)

    def test_all_explicit_guards_required(self):
        for key, value in [
            ("tier", "paid"),
            ("billing_enabled", True),
            ("billing_enabled", None),
            ("key_rotated", False),
            ("key_sha256", "other"),
            ("rpd", 0),
            ("expires_at", (NOW - timedelta(seconds=1)).isoformat()),
            ("expires_at", (NOW + timedelta(days=100)).isoformat()),
        ]:
            with self.subTest(key=key, value=value):
                c = {**self.c, key: value}
                with self.assertRaises(PolicyError):
                    verify_free_tier(self.p, c, KEY, NOW)

    def test_paid_flags_or_unreviewed_model_blocked(self):
        for key, value in [
            ("FREE_ONLY", False),
            ("MAX_SPEND_USD", 1),
            ("ALLOW_PAID_FALLBACK", True),
            ("AI_PROVIDER", "other"),
            ("model", "gemini-pro-latest"),
        ]:
            with self.subTest(key=key), self.assertRaises(PolicyError):
                verify_free_tier({**self.p, key: value}, self.c, KEY, NOW)

    def test_expired_pricing_blocks(self):
        with self.assertRaises(PolicyError):
            verify_free_tier(
                {**self.p, "pricing_checked_on": "2026-01-01"}, self.c, KEY, NOW
            )

    def test_valid_confirmation(self):
        verify_free_tier(self.p, self.c, KEY, NOW)

    def test_shared_free_project_still_requires_no_billing(self):
        c = {**self.c, "dedicated_project": None}
        verify_free_tier(self.p, c, KEY, NOW)
        with self.assertRaises(PolicyError):
            verify_free_tier(self.p, {**c, "billing_enabled": True}, KEY, NOW)

    def test_auth_error_in_400_response_is_classified(self):
        with self.assertRaises(AuthError):
            Gemini.check_response(
                response({"error": {"details": [{"reason": "API_KEY_INVALID"}]}}, 400)
            )

    def test_transport_redacts_token_from_exception(self):
        http = Http()
        http.opener = Mock()
        http.opener.open.side_effect = OSError(
            "https://api.telegram.org/botSECRET/sendMessage"
        )
        with self.assertRaises(KairosError) as caught:
            http.request("POST", "https://api.telegram.org/botSECRET/sendMessage")
        self.assertNotIn("SECRET", str(caught.exception))


class QuotaTests(Base):
    def test_rpd_survives_restart(self):
        self.c["rpd"] = 1
        self.g.reserve(provider="gemini", seconds=100, tokens=100)
        s2 = State(self.s.path)
        g2 = Guard(s2, self.p, self.c, self.clock)
        with self.assertRaises(QuotaError):
            g2.reserve(provider="gemini", seconds=100, tokens=100)

    def test_8_hours_is_not_reset_at_midnight(self):
        self.g.reserve(provider="gemini", seconds=28800)
        self.clock.return_value = NOW + timedelta(hours=13)
        with self.assertRaises(QuotaError):
            self.g.reserve(provider="gemini", seconds=1)
        self.clock.return_value = NOW + timedelta(hours=24, seconds=1)
        self.g.reserve(provider="gemini", seconds=1)

    def test_tpm_and_rpm_block_before_call(self):
        self.c["tpm"] = 100
        with self.assertRaises(QuotaError):
            self.g.reserve(provider="gemini", tokens=101)
        self.assertEqual([], self.s.data["reservations"])
        self.c["rpm"] = 1
        self.g.reserve(provider="gemini")
        with self.assertRaises(QuotaError) as caught:
            self.g.reserve(provider="gemini")
        self.assertLess(
            timestamp(caught.exception.retry_at) - NOW, timedelta(minutes=2)
        )

    def test_daily_token_limit(self):
        self.c["tokens_per_day"] = 1000
        self.g.reserve(provider="gemini", tokens=700)
        with self.assertRaises(QuotaError):
            self.g.reserve(provider="gemini", tokens=301)

    def test_youtube_units(self):
        self.p["youtube_units_per_day"] = 1
        self.g.reserve(provider="youtube", units=1)
        with self.assertRaises(QuotaError):
            self.g.reserve(provider="youtube", units=1)

    def test_dst_and_pacific_day_rollover(self):
        # In October 07:00 UTC is midnight PDT; not midnight UTC/KST.
        self.clock.return_value = datetime(2026, 10, 19, 6, 59, tzinfo=timezone.utc)
        self.c["rpd"] = 1
        self.g.reserve(provider="gemini")
        self.clock.return_value += timedelta(minutes=2)
        self.g.reserve(provider="gemini")

    def test_checkpoint_failure_stops_reservation(self):
        self.s.checkpoint = Mock(side_effect=PolicyError("state_checkpoint_failed"))
        with self.assertRaises(PolicyError):
            self.g.reserve(provider="gemini", seconds=100)


class DiscoveryTests(Base):
    def yt(self, entries):
        yt = Mock()
        yt.api_key = ""
        yt.feed.return_value = entries
        return yt

    def channel(self):
        return {
            "channel_id": CID,
            "verification": "verified_public_channel_page",
            "priority": 50,
        }

    def test_all_32_registered_and_verified(self):
        rows = json.loads((ROOT / "config/channels.yaml").read_text(encoding="utf-8"))[
            "channels"
        ]
        self.assertEqual(32, len(rows))
        self.assertEqual(32, len({r["channel_id"] for r in rows}))
        self.assertTrue(
            all(
                r["verified_at"] and r["verification"].startswith("verified")
                for r in rows
            )
        )

    def test_bootstrap_skips_history_then_discovers_new(self):
        old = {**video(), "published_at": (NOW - timedelta(hours=1)).isoformat()}
        yt = self.yt([old])
        discover(self.s, yt, [self.channel()], self.clock)
        self.assertFalse(self.s.data["videos"])
        fresh = {**video(), "published_at": (NOW + timedelta(minutes=1)).isoformat()}
        self.clock.return_value = NOW + timedelta(hours=9)
        yt.feed.return_value = [fresh, old]
        discover(self.s, yt, [self.channel()], self.clock)
        self.assertEqual([VID], list(self.s.data["videos"]))
        discover(self.s, yt, [self.channel()], self.clock)
        self.assertEqual(1, len(self.s.data["videos"]))

    def test_32_rss_reads_preserve_baselines_without_32_remote_pushes(self):
        channel_rows = [
            {**self.channel(), "channel_id": "UC" + str(i).zfill(22)} for i in range(32)
        ]
        checkpoint = Mock()
        self.s.checkpoint = checkpoint
        yt = self.yt([])

        def read(cid):
            # A crash at any first read cannot erase the initial cutoff.
            saved = json.loads(self.s.path.read_text(encoding="utf-8"))
            self.assertEqual(32, len(saved["channels"]))
            return []

        yt.feed.side_effect = read
        self.assertEqual([], discover(self.s, yt, channel_rows, self.clock))
        self.assertEqual(32, yt.feed.call_count)
        self.assertLessEqual(checkpoint.call_count, 2)
        restored = State(self.s.path)
        self.assertTrue(
            all(v["last_success"] for v in restored.data["channels"].values())
        )

    def test_failed_initial_discovery_preserves_baseline(self):
        yt = self.yt([])
        yt.feed.side_effect = DiscoveryError()
        discover(self.s, yt, [self.channel()], self.clock)
        self.assertEqual(NOW.isoformat(), self.s.data["channels"][CID]["baseline"])

    def test_feed_gap_never_advances_watermark_without_api(self):
        oldtime = NOW - timedelta(days=3)
        self.s.data["channels"][CID] = {
            "baseline": oldtime.isoformat(),
            "last_success": oldtime.isoformat(),
        }
        errors = discover(self.s, self.yt([video()]), [self.channel()], self.clock)
        self.assertIn("feed_gap_youtube_key_required", errors)
        self.assertEqual(
            oldtime.isoformat(), self.s.data["channels"][CID]["last_success"]
        )
        self.assertIn(VID, self.s.data["videos"])

    def test_feed_gap_uses_paginated_api(self):
        self.s.data["channels"][CID] = {
            "baseline": (NOW - timedelta(days=3)).isoformat(),
            "last_success": None,
        }
        yt = self.yt([video()])
        yt.api_key = "fixture"
        yt.uploads.return_value = [video()]
        self.assertFalse(discover(self.s, yt, [self.channel()], self.clock))
        yt.uploads.assert_called_once()

    def test_official_feed_uc_prefix_variant(self):
        xml = f"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
        <yt:channelId>{CID[2:]}</yt:channelId><link rel="alternate" href="https://www.youtube.com/channel/{CID}"/>
        <entry><yt:videoId>{VID}</yt:videoId><yt:channelId>{CID}</yt:channelId><title>fixture</title>
        <published>{NOW.isoformat()}</published></entry></feed>"""
        http = Mock()
        http.request.return_value = Response(200, xml.encode(), {})
        yt = YouTube(http, self.g)
        self.assertEqual(VID, yt.feed(CID)[0]["video_id"])
        http.request.return_value = Response(
            200, xml.replace(CID[2:], "wrong").encode(), {}
        )
        with self.assertRaises(DiscoveryError):
            yt.feed(CID)

    def test_unknown_video_duration_is_never_guessed(self):
        details = {"videoId": VID, "channelId": CID}
        http = Mock()
        http.request.return_value = Response(
            200,
            (
                "var ytInitialPlayerResponse = "
                + json.dumps(
                    {"videoDetails": details, "playabilityStatus": {"status": "OK"}}
                )
            ).encode(),
            {},
        )
        with self.assertRaises(VideoError):
            YouTube(http, self.g).metadata(video())

    def test_official_api_metadata_uses_separate_key_and_reserves_unit(self):
        http = Mock()
        http.request.return_value = response(
            {
                "items": [
                    {
                        "id": VID,
                        "snippet": {
                            "channelId": CID,
                            "title": "fixture",
                            "liveBroadcastContent": "none",
                        },
                        "contentDetails": {"duration": "PT1H2M3S"},
                        "status": {
                            "privacyStatus": "public",
                            "uploadStatus": "processed",
                        },
                    }
                ]
            }
        )
        yt = YouTube(http, self.g, "separate-youtube-fixture")
        self.assertEqual(3723, yt.metadata(video()))
        self.assertEqual(3723, yt.metadata(video()))
        self.assertEqual(1, http.request.call_count)
        self.assertEqual(
            "separate-youtube-fixture",
            http.request.call_args.kwargs["headers"]["x-goog-api-key"],
        )
        self.assertEqual(1, self.s.data["reservations"][0]["units"])

    def test_api_rejects_unlisted_live_missing_and_wrong_channel(self):
        item = {
            "id": VID,
            "snippet": {
                "channelId": CID,
                "title": "fixture",
                "liveBroadcastContent": "none",
            },
            "contentDetails": {"duration": "PT2M"},
            "status": {"privacyStatus": "public", "uploadStatus": "processed"},
        }
        variants = [
            {**item, "status": {**item["status"], "privacyStatus": "unlisted"}},
            {**item, "snippet": {**item["snippet"], "liveBroadcastContent": "live"}},
            {**item, "contentDetails": {"duration": "unknown"}},
            {**item, "snippet": {**item["snippet"], "channelId": "UC" + "z" * 22}},
        ]
        for changed in variants:
            with self.subTest(item=changed), self.assertRaises(VideoError):
                http = Mock()
                http.request.return_value = response({"items": [changed]})
                YouTube(http, self.g, "separate-youtube-fixture").metadata(video())
        http = Mock()
        http.request.return_value = response({"items": []})
        with self.assertRaises(VideoError):
            YouTube(http, self.g, "fixture").metadata(video())

    def test_cloud_login_requirement_requests_api_key_without_logging_in(self):
        http = Mock()
        http.request.return_value = Response(
            200,
            b'var ytInitialPlayerResponse = {"playabilityStatus":{"status":"LOGIN_REQUIRED"}}',
            {},
        )
        with self.assertRaises(VideoError) as caught:
            YouTube(http, self.g).metadata(video())
        self.assertEqual("youtube_metadata_api_key_required", caught.exception.code)
        self.assertEqual(1, http.request.call_count)

    def test_smoke_snapshot_is_exact_video_recent_and_never_a_summary(self):
        current = datetime.now(timezone.utc)
        snapshot = {
            "video_id": VID,
            "channel_id": CID,
            "title": "fixture",
            "duration_seconds": 100,
            "verified_at": current.isoformat(),
            "source": video()["url"],
            "public": True,
            "playable": True,
        }
        http = Mock()
        self.assertEqual(
            100, YouTube(http, self.g, verified_metadata=snapshot).metadata(video())
        )
        http.request.assert_not_called()
        for changed in [
            {"verified_at": (current - timedelta(days=2)).isoformat()},
            {"public": False},
            {"source": "wrong"},
            {"channel_id": "UC" + "z" * 22},
        ]:
            with self.subTest(changed=changed), self.assertRaises(VideoError):
                YouTube(
                    http, self.g, verified_metadata={**snapshot, **changed}
                ).metadata(video())


class GeminiTests(Base):
    def test_visual_sampling_recounts_same_whole_video_without_audio_clipping(self):
        http = Mock()
        generated = response(
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json.dumps(summary())}]},
                    }
                ]
            }
        )
        http.request.side_effect = [
            response({"totalTokens": 1000}),
            generated,
            response({"totalTokens": 800}),
            generated,
        ]
        v = video()
        client = Gemini(http, self.g, self.p, KEY)
        client.summarize(v, 100)
        v["video_fps"] = 0.5
        client.summarize(v, 100)
        calls = http.request.call_args_list
        self.assertTrue(calls[2].args[1].endswith(":countTokens"))
        counted = calls[2].kwargs["payload"]["generateContentRequest"]["contents"]
        content = calls[3].kwargs["payload"]["contents"]
        self.assertEqual(counted, content)
        media = content[0]["parts"][1]
        self.assertEqual({"fps": 0.5}, media["videoMetadata"])
        self.assertEqual(v["url"], media["fileData"]["fileUri"])
        self.assertEqual(400, sum(r["seconds"] for r in self.s.data["reservations"]))
        v["video_fps"] = 0.01
        with self.assertRaises(VideoError):
            client.summarize(v, 100)
        self.assertEqual(4, http.request.call_count)

    def test_explicit_video_time_is_converted_without_guessing_mmss_numbers(self):
        self.assertEqual(225, video_timestamp_seconds("03:45"))
        self.assertEqual(1010, video_timestamp_seconds("16:50"))
        self.assertEqual(4530, video_timestamp_seconds("01:15:30"))
        for invalid in (345, "345", "03:99", "01:60:00", "-1:00"):
            with self.subTest(invalid=invalid), self.assertRaises(VideoError):
                video_timestamp_seconds(invalid)

    def test_explicit_timestamp_conversion_keeps_the_actual_coverage_checks(self):
        data = summary()
        for b in data["bullets"]:
            e = b["evidence"][0]
            for name in ("start", "end"):
                n = e.pop(name + "_seconds")
                e[name + "_time"] = f"{n // 60:02d}:{n % 60:02d}"
        canonical_evidence_times(data)
        validate_summary(data, video(), 100)
        data["bullets"][-1]["evidence"][0].update({"end_time": "03:45"})
        with self.assertRaises(VideoError):
            canonical_evidence_times(data)

    def test_one_rpm_account_reuses_count_on_next_available_run(self):
        self.c["rpm"] = 1
        v = video()
        self.s.data["videos"][VID] = v
        http = Mock()
        http.request.side_effect = [
            response({"totalTokens": 1000}),
            response(
                {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": json.dumps(summary())}]},
                        }
                    ]
                }
            ),
        ]
        client = Gemini(http, self.g, self.p, KEY)
        with self.assertRaises(QuotaError):
            client.summarize(v, 100)
        self.assertEqual(1, http.request.call_count)
        self.clock.return_value = NOW + timedelta(minutes=2)
        client.summarize(v, 100)
        self.assertEqual(2, http.request.call_count)
        self.assertTrue(http.request.call_args.args[1].endswith(":generateContent"))

    def test_full_video_summary_has_evidence(self):
        self.assertEqual(summary(), validate_summary(summary(), video(), 100))

    def test_insufficient_content_not_completed(self):
        for change in (
            {"content_access": "insufficient"},
            {"coverage_complete": False},
            {"observed_duration_seconds": 30},
            {"video_id": "other"},
        ):
            with self.subTest(change=change), self.assertRaises(VideoError):
                validate_summary({**summary(), **change}, video(), 100)

    def test_missing_late_coverage_rejected(self):
        data = summary()
        for b in data["bullets"]:
            b["evidence"][0]["start_seconds"] = 0
            b["evidence"][0]["end_seconds"] = 10
        with self.assertRaises(VideoError):
            validate_summary(data, video(), 100)

    def test_out_of_video_evidence_is_rejected_without_clipping(self):
        data = summary()
        data["bullets"][-1]["evidence"][0]["end_seconds"] = 345
        with self.assertRaises(VideoError):
            validate_summary(data, video(), 100)
        self.assertEqual(345, data["bullets"][-1]["evidence"][0]["end_seconds"])

    def test_rest_video_and_count_payload_match_and_reserve_before_call(self):
        http = Mock()
        seen = []

        def request(method, url, **kw):
            seen.append((url, kw["payload"]))
            self.assertTrue(self.s.path.exists())
            if url.endswith(":countTokens"):
                return response({"totalTokens": 1000})
            return response(
                {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": json.dumps(summary())}]},
                        }
                    ],
                    "usageMetadata": {"totalTokenCount": 1500},
                }
            )

        http.request.side_effect = request
        result = Gemini(http, self.g, self.p, KEY).summarize(video(), 100)
        self.assertEqual(5, len(result["bullets"]))
        count_request = seen[0][1]["generateContentRequest"]
        self.assertEqual(count_request["contents"], seen[1][1]["contents"])
        self.assertEqual(
            count_request["systemInstruction"], seen[1][1]["systemInstruction"]
        )
        self.assertEqual(
            count_request["generationConfig"], seen[1][1]["generationConfig"]
        )
        schema = seen[1][1]["generationConfig"]["responseSchema"]
        self.assertIn("insufficient", schema["properties"]["content_access"]["enum"])
        self.assertEqual(
            video()["url"], seen[1][1]["contents"][0]["parts"][1]["fileData"]["fileUri"]
        )
        self.assertEqual(200, sum(r["seconds"] for r in self.s.data["reservations"]))

    def test_429_does_not_try_paid_model(self):
        http = Mock()
        http.request.return_value = response({}, 429)
        with self.assertRaises(QuotaError):
            Gemini(http, self.g, self.p, KEY).summarize(video(), 100)
        self.assertEqual(1, http.request.call_count)

    def test_truncated_response_rejected(self):
        http = Mock()
        http.request.side_effect = [
            response({"totalTokens": 1000}),
            response({"candidates": [{"finishReason": "MAX_TOKENS"}]}),
        ]
        with self.assertRaises(GeminiError):
            Gemini(http, self.g, self.p, KEY).summarize(video(), 100)


class DeliveryTests(Base):
    def test_rate_limit_wait_is_saved_and_no_partial_message_is_marked_sent(self):
        http = Mock()
        http.request.return_value = response(
            {"parameters": {"retry_after": 90}, "description": "Too Many Requests"},
            429,
        )
        v = {**video(), "status": "ready", "parts": ["fixture"]}
        self.s.data["videos"][VID] = v
        with self.assertRaises(TelegramRateLimit) as caught:
            deliver(self.s, v, Telegram(http, "fixture-token", "123"))
        self.assertEqual(90, caught.exception.retry_after)
        saved = State(self.s.path).data["videos"][VID]
        self.assertEqual("ready", saved["status"])
        self.assertEqual([], saved["sent_parts"])
        self.assertGreater(timestamp(saved["retry_at"]), datetime.now(timezone.utc))

    def test_style_and_emoji_length(self):
        data = summary()
        for b in data["bullets"]:
            b["text"] *= 30
        messages = render(video(), data)
        self.assertGreater(len(messages), 1)
        self.assertEqual(video()["url"], messages[0]["preview_url"])
        self.assertIn(video()["url"], messages[0]["text"])
        self.assertTrue(
            all(
                m["text"].startswith("📊 ")
                and "▶️ " in m["text"]
                and video()["url"] not in m["text"]
                and "preview_url" not in m
                for m in messages[1:]
            )
        )
        self.assertTrue(all(units(m["text"]) <= 4096 for m in messages))

    def test_fixed_chat_and_plain_text_only(self):
        http = Mock()
        http.request.return_value = response(
            {"ok": True, "result": {"message_id": 42, "chat": {"id": 123}}}
        )
        tg = Telegram(http, "fixture-token", "123")
        self.assertEqual(42, tg.send("fixture"))
        payload = http.request.call_args.kwargs["payload"]
        self.assertEqual("123", payload["chat_id"])
        self.assertNotIn("parse_mode", payload)
        self.assertEqual({"is_disabled": True}, payload["link_preview_options"])

    def test_provider_rejection_is_classified_without_echoing_secret_or_raw_body(self):
        http = Mock()
        http.request.return_value = response(
            {"description": "Unauthorized fixture-secret-do-not-log"}, 401
        )
        tg = Telegram(http, "fixture-token", "123")
        with self.assertRaises(TelegramError) as caught:
            tg.send("fixture")
        self.assertEqual("telegram_http_401_token_invalid", caught.exception.code)
        self.assertNotIn("fixture-secret", str(caught.exception))
        http.request.return_value = Response(400, b"not-json-secret", {})
        with self.assertRaises(TelegramError) as caught:
            tg.send("fixture")
        self.assertEqual("telegram_http_400_request_rejected", caught.exception.code)

    def test_youtube_card_requests_large_preview_above_channel_and_title(self):
        http = Mock()
        http.request.return_value = response(
            {"ok": True, "result": {"message_id": 42, "chat": {"id": 123}}}
        )
        source = {**video(), "channel_name": "테스트 채널"}
        card = render(source, summary())[0]
        tg = Telegram(http, "fixture-token", "123")
        tg.send(card["text"], preview_url=card["preview_url"])
        payload = http.request.call_args.kwargs["payload"]
        self.assertIn("📺 테스트 채널", payload["text"])
        self.assertIn(source["title"], payload["text"])
        self.assertEqual(
            {
                "is_disabled": False,
                "url": source["url"],
                "prefer_large_media": True,
                "show_above_text": True,
            },
            payload["link_preview_options"],
        )
        with self.assertRaises(TelegramError):
            tg.send("fixture", preview_url=source["url"])
        self.assertEqual(1, http.request.call_count)

    def setup_saved_preview(self):
        v = {
            **video(),
            "status": "sent",
            "duration_seconds": 100,
            "summary": summary(),
            "parts": ["original"],
            "sent_parts": [5],
        }
        self.s.data["videos"][VID] = v
        return v, [{"channel_id": CID, "name": "테스트 채널"}]

    def test_preview_test_reuses_summary_once_without_changing_original(self):
        v, rows = self.setup_saved_preview()
        tg = Mock()
        tg.send.side_effect = [11, 12]
        reservations = list(self.s.data["reservations"])
        result = send_preview_test(self.s, VID, rows, tg)
        self.assertEqual(2, result["message_count"])
        self.assertEqual([5], v["sent_parts"])
        self.assertEqual(reservations, self.s.data["reservations"])
        restarted = State(self.s.path)
        send_preview_test(restarted, VID, rows, tg)
        self.assertEqual(2, tg.send.call_count)
        self.assertEqual(
            video()["url"], tg.send.call_args_list[0].kwargs["preview_url"]
        )
        self.assertIsNone(tg.send.call_args_list[1].kwargs["preview_url"])

    def test_preview_test_resumes_only_summary_after_definite_second_failure(self):
        _, rows = self.setup_saved_preview()
        tg = Mock()
        tg.send.side_effect = [11, TelegramError()]
        with self.assertRaises(TelegramError):
            send_preview_test(self.s, VID, rows, tg)
        second = Mock()
        second.send.return_value = 12
        send_preview_test(State(self.s.path), VID, rows, second)
        self.assertEqual(1, second.send.call_count)
        self.assertIn("▶️", second.send.call_args.args[0])

    def test_preview_test_uncertain_card_is_not_resent_after_restart(self):
        _, rows = self.setup_saved_preview()
        tg = Mock()
        tg.send.side_effect = UncertainDelivery()
        with self.assertRaises(UncertainDelivery):
            send_preview_test(self.s, VID, rows, tg)
        restarted = State(self.s.path)
        with self.assertRaises(PolicyError):
            send_preview_test(restarted, VID, rows, tg)
        self.assertEqual(1, tg.send.call_count)
        outbox = next(iter(restarted.data["presentations"].values()))
        outbox["status"] = "sending"
        restarted.recover()
        self.assertEqual("delivery_uncertain", outbox["status"])

    def test_failure_after_first_part_resumes_remaining_only(self):
        v = video()
        v["parts"], v["status"] = ["part one", "part two"], "ready"
        self.s.data["videos"][VID] = v
        tg = Mock()
        tg.send.side_effect = [11, TelegramError()]
        with self.assertRaises(TelegramError):
            deliver(self.s, v, tg)
        s2 = State(self.s.path)
        v2 = s2.data["videos"][VID]
        tg2 = Mock()
        tg2.send.return_value = 12
        deliver(s2, v2, tg2)
        tg2.send.assert_called_once_with("part two")
        self.assertEqual("sent", v2["status"])
        self.assertEqual([11, 12], v2["sent_parts"])

    def test_timeout_remains_uncertain_across_restart(self):
        v = video()
        v["parts"] = ["one"]
        self.s.data["videos"][VID] = v
        tg = Mock()
        tg.send.side_effect = UncertainDelivery()
        with self.assertRaises(UncertainDelivery):
            deliver(self.s, v, tg)
        s2 = State(self.s.path)
        s2.recover()
        self.assertEqual("delivery_uncertain", s2.data["videos"][VID]["status"])

    def test_killed_while_sending_is_not_auto_resent(self):
        v = {**video(), "status": "sending"}
        self.s.data["videos"][VID] = v
        self.s.save()
        self.s.recover()
        self.assertEqual("delivery_uncertain", v["status"])


class RunnerTests(Base):
    def test_missing_youtube_key_allows_saved_delivery_but_no_ai(self):
        yt, gemini, tg = self.setup_clients()
        yt.api_key = ""
        target = self.s.data["videos"][VID]
        target.update(status="ready", parts=["previously generated summary"])
        result = run(
            self.s,
            [],
            yt,
            gemini,
            tg,
            self.p,
            self.c,
            KEY,
            self.clock,
            require_youtube_api=True,
        )
        self.assertTrue(result["processing_held"])
        self.assertEqual(1, result["sent"])
        self.assertEqual("sent", target["status"])
        gemini.check_model.assert_not_called()
        gemini.summarize.assert_not_called()

    def test_ready_summary_waits_until_telegram_retry_time_without_ai(self):
        yt, gemini, tg = self.setup_clients()
        v = self.s.data["videos"][VID]
        v.update(
            status="ready",
            parts=["saved summary"],
            retry_at=(NOW + timedelta(minutes=2)).isoformat(),
        )
        self.execute(yt, gemini, tg)
        gemini.summarize.assert_not_called()
        self.assertEqual("ready", v["status"])
        self.assertEqual(1, tg.send.call_count)  # Status briefing only.

    def test_manual_request_skips_channel_discovery_and_other_videos(self):
        yt, gemini, tg = self.setup_clients()
        target = self.s.data["videos"][VID]
        target["channel_name"] = "요청한 외부 채널"
        other = {**video(), "video_id": "zyxwvutsrqp", "priority": 9999}
        self.s.data["videos"][other["video_id"]] = other
        with patch("kairos_youtube.runner.discover") as discovery:
            run(
                self.s,
                [],
                yt,
                gemini,
                tg,
                self.p,
                self.c,
                KEY,
                self.clock,
                only_video_id=VID,
                max_videos=1,
                skip_discovery=True,
            )
        discovery.assert_not_called()
        gemini.summarize.assert_called_once()
        self.assertEqual("pending", other["status"])
        self.assertEqual({}, self.s.data["channels"])
        self.assertIn("요청한 외부 채널", tg.send.call_args_list[0].args[0])
        with self.assertRaises(PolicyError):
            run(
                self.s,
                [],
                yt,
                gemini,
                tg,
                self.p,
                self.c,
                KEY,
                self.clock,
                skip_discovery=True,
            )

    def test_manual_request_retains_free_tier_and_quota_checks(self):
        yt, gemini, tg = self.setup_clients()
        missing = run(
            self.s,
            [],
            yt,
            gemini,
            tg,
            self.p,
            self.c,
            "",
            self.clock,
            only_video_id=VID,
            skip_discovery=True,
        )
        self.assertEqual("gemini_key_missing", missing["reason"])
        gemini.summarize.assert_not_called()
        gemini.summarize.side_effect = QuotaError()
        result = run(
            self.s,
            [],
            yt,
            gemini,
            tg,
            self.p,
            self.c,
            KEY,
            self.clock,
            only_video_id=VID,
            skip_discovery=True,
        )
        self.assertEqual("free_quota_exhausted", result["reason"])
        self.assertEqual("pending", self.s.data["videos"][VID]["status"])

    def test_cloud_monitor_without_youtube_key_keeps_queue_and_never_calls_ai(self):
        yt, gemini, tg = self.setup_clients()
        yt.api_key = ""
        result = run(
            self.s,
            [],
            yt,
            gemini,
            tg,
            self.p,
            self.c,
            KEY,
            self.clock,
            require_youtube_api=True,
        )
        self.assertTrue(result["processing_held"])
        self.assertEqual("pending", self.s.data["videos"][VID]["status"])
        gemini.check_model.assert_not_called()
        gemini.summarize.assert_not_called()
        self.assertIn("YouTube 조회 키 등록 대기", tg.send.call_args.args[0])

    def test_four_daily_briefings_are_distinct_and_each_rerun_is_suppressed(self):
        tg = Mock()
        tg.send.return_value = 42
        result = {
            "sent": 0,
            "pending": 0,
            "delivery_uncertain": 0,
            "errors": [],
            "reason": None,
        }
        for hour in (2, 6, 11, 14):
            started = NOW.replace(hour=hour, minute=30 if hour == 2 else 0)
            send_briefing(self.s, tg, started, result)
            send_briefing(self.s, tg, started + timedelta(minutes=20), result)
        self.assertEqual(4, tg.send.call_count)
        self.assertEqual(4, len(self.s.data["briefings"]))

    def test_briefing_slots_roll_to_previous_run_before_first_slot(self):
        before_first = NOW.replace(hour=2, minute=29)
        prior_evening = (NOW - timedelta(days=1)).replace(hour=14, minute=0)
        self.assertEqual(briefing_slot(prior_evening), briefing_slot(before_first))
        self.assertNotEqual(
            briefing_slot(before_first), briefing_slot(NOW.replace(hour=2, minute=30))
        )
        kst = timezone(timedelta(hours=9))
        self.assertEqual(briefing_slot(NOW), briefing_slot(NOW.astimezone(kst)))

    def test_rejected_evidence_is_reported_as_processing_failure(self):
        yt, gemini, tg = self.setup_clients()
        gemini.summarize.side_effect = VideoError("invalid_video_evidence")
        result = self.execute(yt, gemini, tg)
        self.assertEqual(["invalid_video_evidence"], result["processing_errors"])
        self.assertEqual(0, result["sent"])
        self.assertEqual("pending", self.s.data["videos"][VID]["status"])
        self.assertIn("일부 처리 보류", tg.send.call_args.args[0])

    def test_no_telegram_credentials_means_no_google_requests(self):
        yt, gemini, _ = self.setup_clients()
        result = self.execute(yt, gemini, None)
        self.assertEqual("telegram_secrets_missing_or_invalid", result["reason"])
        gemini.check_model.assert_not_called()
        gemini.summarize.assert_not_called()

    def test_smoke_processes_only_requested_video(self):
        yt, gemini, tg = self.setup_clients()
        other = {**video(), "video_id": "zyxwvutsrqp", "priority": 9999}
        self.s.data["videos"][other["video_id"]] = other
        run(
            self.s,
            [],
            yt,
            gemini,
            tg,
            self.p,
            self.c,
            KEY,
            self.clock,
            only_video_id=VID,
            max_videos=1,
        )
        self.assertEqual(VID, gemini.summarize.call_args.args[0]["video_id"])
        self.assertEqual("pending", other["status"])

    def setup_clients(self):
        yt, gemini, tg = Mock(), Mock(), Mock()
        gemini.summarize.return_value = summary()
        gemini.guard = self.g
        yt.metadata.return_value = 100
        tg.send.return_value = 42
        self.s.data["videos"][VID] = video()
        return yt, gemini, tg

    def execute(self, yt, gemini, tg, key=KEY, c=None):
        return run(
            self.s,
            [],
            yt,
            gemini,
            tg,
            self.p,
            c if c is not None else self.c,
            key,
            self.clock,
        )

    def test_missing_key_continues_state_but_no_google_calls(self):
        yt, gemini, tg = self.setup_clients()
        result = self.execute(yt, gemini, tg, key="")
        self.assertEqual("gemini_key_missing", result["reason"])
        gemini.check_model.assert_not_called()
        gemini.summarize.assert_not_called()
        self.assertEqual("pending", self.s.data["videos"][VID]["status"])

    def test_rerun_neither_analyzes_nor_sends_again(self):
        yt, gemini, tg = self.setup_clients()
        self.execute(yt, gemini, tg)
        self.execute(yt, gemini, tg)
        gemini.summarize.assert_called_once()
        # One preview card, one summary and one briefing for the UTC slot.
        self.assertEqual(3, tg.send.call_count)

    def test_auth_failure_persists_pause(self):
        yt, gemini, tg = self.setup_clients()
        gemini.summarize.side_effect = AuthError()
        self.execute(yt, gemini, tg)
        self.execute(yt, gemini, tg)
        gemini.summarize.assert_called_once()
        self.assertEqual(fingerprint(KEY), self.s.data["pause"]["fingerprint"])

    def test_free_quota_exhaustion_keeps_pending(self):
        yt, gemini, tg = self.setup_clients()
        gemini.summarize.side_effect = QuotaError()
        result = self.execute(yt, gemini, tg)
        self.assertEqual("free_quota_exhausted", result["reason"])
        self.assertEqual("pending", self.s.data["videos"][VID]["status"])
        self.assertIsNotNone(self.s.data["videos"][VID]["retry_at"])

    def test_resume_ready_summary_without_reanalysis(self):
        yt, gemini, tg = self.setup_clients()
        v = self.s.data["videos"][VID]
        v["status"], v["parts"] = "ready", ["already analyzed"]
        self.execute(yt, gemini, tg)
        gemini.summarize.assert_not_called()
        self.assertEqual("sent", v["status"])

    def test_uncertain_summary_is_never_resent(self):
        yt, gemini, tg = self.setup_clients()
        self.s.data["videos"][VID]["status"] = "delivery_uncertain"
        self.execute(yt, gemini, tg)
        gemini.summarize.assert_not_called()
        self.assertEqual(1, tg.send.call_count)  # Administrator briefing only.

    def test_storage_cap_blocks_checkpoint(self):
        s = State(Path(self.temp.name) / "tiny.json", max_bytes=10)
        with self.assertRaises(PolicyError):
            s.save()


if __name__ == "__main__":
    unittest.main()
