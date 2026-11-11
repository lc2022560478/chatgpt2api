from __future__ import annotations

import unittest

from services.realtime_monitor_service import RealtimeMonitorService


class RealtimeMonitorServiceTests(unittest.TestCase):
    def test_snapshot_tracks_lifecycle_and_retry_events(self):
        service = RealtimeMonitorService(recent_capacity=2, event_capacity=4)

        service.start(
            "call-1",
            endpoint="/v1/images/generations",
            model="gpt-image-2",
            summary="image generation",
            role="admin",
            key_name="primary",
        )
        service.stage("call-1", "account_selected", account_email="one@example.test", attempt=1, max_attempts=3)
        service.stage(
            "call-1",
            "retrying_account",
            account_email="one@example.test",
            previous_account_email="one@example.test",
            retry_count=1,
            attempt=1,
            max_attempts=3,
            error="upstream unavailable",
        )

        active = service.snapshot()
        self.assertEqual(active["summary"]["active"], 1)
        self.assertEqual(active["active"][0]["stage"], "retrying_account")
        self.assertEqual(active["active"][0]["retry_count"], 1)

        service.finish("call-1", "success", account_email="two@example.test")
        completed = service.snapshot()
        self.assertEqual(completed["summary"]["active"], 0)
        self.assertEqual(completed["summary"]["completed"], 1)
        self.assertEqual(completed["summary"]["success"], 1)
        self.assertEqual(completed["summary"]["account_switches"], 1)
        self.assertEqual(completed["recent"][0]["account_email"], "two@example.test")
        self.assertNotIn("error", completed["recent"][0])
        self.assertNotIn("error", completed["events"][0])
        retry = next(event for event in completed["events"] if event["event"] == "retrying_account")
        self.assertEqual(retry["error"], "upstream unavailable")

    def test_successful_retry_clears_failure_fields_without_losing_history(self):
        service = RealtimeMonitorService()
        failure = dict(error="stream timed out", code="upstream_timeout", failure_code="timeout",
                       error_type="server_error", status_code=502)
        service.start("call-1", endpoint="/v1/images/generations", model="gpt-image-2", summary="image")
        service.stage("call-1", "retrying_account", index=1, total=1, retry_count=1, **failure)
        service.stage("call-1", "receiving_image", index=1, attempt=2)
        service.stage("call-1", "image_ready", index=1, attempt=2)
        image = service.snapshot()["active"][0]["images"]["1"]
        self.assertEqual(image["status"], "success")
        for key in failure:
            self.assertNotIn(key, image)
        # Even a caller passing an earlier attempt's diagnostics cannot label success as failure.
        service.finish("call-1", "success", **failure)
        snapshot = service.snapshot()
        for key in failure:
            self.assertNotIn(key, snapshot["recent"][0])
            for event in snapshot["events"]:
                if event["event"] in {"completed", "image_ready", "receiving_image"}:
                    self.assertNotIn(key, event)
        retry = next(event for event in snapshot["events"] if event["event"] == "retrying_account")
        self.assertEqual(retry["error"], failure["error"])
        self.assertEqual(snapshot["summary"]["account_switches"], 1)

    def test_failed_request_preserves_final_error_and_other_image_failure(self):
        service = RealtimeMonitorService()
        service.stage("call-1", "failed", index=1, error="first image failed", code="upstream_error")
        service.stage("call-1", "retrying_account", index=2, error="temporary failure")
        service.stage("call-1", "image_ready", index=2)
        service.finish("call-1", "failed", error="first image failed", code="upstream_error")
        snapshot = service.snapshot()
        record = snapshot["recent"][0]
        self.assertEqual(record["error"], "first image failed")
        self.assertEqual(record["images"]["1"]["error"], "first image failed")
        self.assertNotIn("error", record["images"]["2"])
        self.assertEqual(snapshot["events"][0]["error"], "first image failed")

    def test_captured_success_does_not_reintroduce_retry_error(self):
        service = RealtimeMonitorService()
        service.stage("call-1", "retrying_account", index=1, error="timeout", code="upstream_timeout")
        service.capture_image_attempts("call-1", [{"slot": 1, "status": "success", "returned_result": True,
                                                  "error": "timeout", "code": "upstream_timeout", "attempt": 2}])
        image = service.snapshot()["active"][0]["images"]["1"]
        self.assertEqual(image["account_attempt"], 2)
        self.assertNotIn("error", image)
        self.assertNotIn("code", image)

    def test_finish_is_idempotent(self):
        service = RealtimeMonitorService()
        service.start("call-1", endpoint="/v1/chat/completions", model="auto", summary="chat")
        service.finish("call-1", "failed", error="timeout")
        service.finish("call-1", "success")

        snapshot = service.snapshot()
        self.assertEqual(snapshot["summary"]["completed"], 1)
        self.assertEqual(snapshot["summary"]["failed"], 1)
        self.assertEqual(snapshot["recent"][0]["status"], "failed")

    def test_late_stage_does_not_revive_finished_call(self):
        service = RealtimeMonitorService()
        service.start("call-1", endpoint="/v1/chat/completions", model="auto", summary="chat")
        service.finish("call-1", "success")
        event_count = len(service.snapshot()["events"])

        service.stage("call-1", "running", error="late callback")

        snapshot = service.snapshot()
        self.assertEqual(snapshot["summary"]["active"], 0)
        self.assertEqual(snapshot["summary"]["completed"], 1)
        self.assertEqual(len(snapshot["events"]), event_count)

    def test_capture_image_attempts_only_keeps_diagnostic_fields(self):
        service = RealtimeMonitorService()
        service.start("call-1", endpoint="/v1/images/edits", model="gpt-image-2", summary="image")
        service.capture_image_attempts(
            "call-1",
            [
                {
                    "slot": 1,
                    "attempt": 2,
                    "max_attempts": 3,
                    "status": "failed",
                    "account_email": "image@example.test",
                    "error": "timeout",
                    "access_token": "must-not-leak",
                    "prompt": "must-not-leak",
                    "raw_error": "must-not-leak",
                    "metrics": {"poll_wait_ms": 1200, "secret_ms": 9},
                }
            ],
        )

        image = service.snapshot()["active"][0]["images"]["1"]
        self.assertEqual(image["account_attempt"], 2)
        self.assertEqual(image["max_account_attempts"], 3)
        self.assertEqual(image["metrics"]["poll_wait_ms"], 1200)
        self.assertNotIn("access_token", image)
        self.assertNotIn("prompt", image)
        self.assertNotIn("raw_error", image)
        self.assertNotIn("secret_ms", image["metrics"])


if __name__ == "__main__":
    unittest.main()
