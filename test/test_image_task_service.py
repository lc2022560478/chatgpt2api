from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services.image_task_service import ImageTaskService
from services.realtime_monitor_service import RealtimeMonitorService


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None) -> ImageTaskService:
        return ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
        )

    def test_default_service_does_not_impose_a_global_worker_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock = threading.Lock()
            all_started = threading.Event()
            release = threading.Event()
            active = 0

            def handler(_payload):
                nonlocal active
                with lock:
                    active += 1
                    if active == 3:
                        all_started.set()
                try:
                    release.wait(timeout=2.0)
                    return {"data": [{"url": "http://example.test/image.png"}]}
                finally:
                    with lock:
                        active -= 1

            service = ImageTaskService(
                Path(tmp_dir) / "image_tasks.json",
                generation_handler=handler,
                edit_handler=handler,
                retention_days_getter=lambda: 30,
            )
            for index in range(3):
                service.submit_generation(
                    OWNER,
                    client_task_id=f"unbounded-{index}",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                )

            try:
                self.assertTrue(all_started.wait(timeout=1.0))
            finally:
                release.set()
            for index in range(3):
                wait_for_task(service, OWNER, f"unbounded-{index}", "success")

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertEqual(result["items"][0]["data"][0]["url"], "http://example.test/image.png")

    def test_generation_task_reports_monitor_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            monitor = RealtimeMonitorService()
            payloads: list[dict[str, object]] = []

            def handler(payload):
                payloads.append(dict(payload))
                return {
                    "data": [{"url": "http://example.test/image.png"}],
                    "_account_email": "image@example.test",
                }

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            with (
                mock.patch("services.realtime_monitor_service.realtime_monitor_service", monitor),
                mock.patch("services.image_task_service.log_service.add"),
            ):
                service.submit_generation(
                    OWNER,
                    client_task_id="monitor-task",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )
                wait_for_task(service, OWNER, "monitor-task", "success")

            snapshot = monitor.snapshot()
            self.assertTrue(payloads[0].get("_monitor_call_id"))
            self.assertEqual(snapshot["summary"]["success"], 1)
            self.assertEqual(snapshot["recent"][0]["account_email"], "image@example.test")

    def test_resume_poll_uses_original_account_context(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            key = "owner-1:resume-task"
            with service._lock:
                service._tasks[key] = {
                    "id": "resume-task",
                    "owner_id": "owner-1",
                    "status": "error",
                    "mode": "generate",
                    "model": "gpt-image-2",
                    "size": "",
                    "quality": "auto",
                    "created_at": "2099-01-01 00:00:00",
                    "updated_at": "2099-01-01 00:00:00",
                    "error": "图片生成超时",
                    "conversation_id": "conversation-1",
                    "account_email": "image@example.test",
                    "account_user_id": "user-1",
                }
                service._save_locked()
            service = self.make_service(path)

            backend_tokens: list[str] = []

            class FakeBackend:
                def __init__(self, access_token=""):
                    backend_tokens.append(access_token)

                def _poll_image_results(self, _conversation_id, _timeout):
                    return ["file-1"], []

                def resolve_conversation_image_urls(self, _conversation_id, _file_ids, _sediment_ids, poll=False):
                    return ["https://example.test/image.png"]

                def download_image_bytes(self, _urls):
                    return [b"image-bytes"]

                def close(self):
                    pass

            with (
                mock.patch(
                    "services.account_service.account_service.list_accounts",
                    return_value=[{
                        "access_token": "current-token",
                        "email": "image@example.test",
                        "user_id": "user-1",
                    }],
                ),
                mock.patch("services.openai_backend_api.OpenAIBackendAPI", FakeBackend),
                mock.patch(
                    "services.protocol.conversation.format_image_result",
                    return_value={"data": [{"url": "http://example.test/image.png"}]},
                ),
                mock.patch("services.image_task_service.log_service.add"),
            ):
                queued = service.resume_poll(OWNER, "resume-task", 5.0)
                self.assertEqual(queued["status"], "queued")
                task = wait_for_task(service, OWNER, "resume-task", "success")

            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(backend_tokens, ["current-token"])
            self.assertNotIn("current-token", service.path.read_text(encoding="utf-8"))

    def test_startup_marks_unfinished_tasks_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))


if __name__ == "__main__":
    unittest.main()
