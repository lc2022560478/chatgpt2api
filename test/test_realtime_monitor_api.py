from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.system as system_module
from services.config import config


class RealtimeMonitorApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(system_module.create_router("test"))
        self.client = TestClient(app)

    def test_admin_can_fetch_realtime_monitor_snapshot(self):
        snapshot = {
            "updated_at": "2026-07-25T12:00:00+08:00",
            "summary": {
                "active": 1,
                "completed": 2,
                "success": 1,
                "failed": 1,
                "success_rate": 50.0,
                "account_switches": 1,
            },
            "active": [],
            "recent": [],
            "events": [],
        }
        stats = {"total": 3, "active": 2, "total_quota": 9}
        with (
            mock.patch.object(system_module.realtime_monitor_service, "snapshot", return_value=snapshot),
            mock.patch("services.account_service.account_service.get_stats", return_value=stats),
        ):
            response = self.client.get(
                "/api/monitor/realtime",
                headers={"Authorization": f"Bearer {config.auth_key}"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["summary"]["active"], 1)
        self.assertEqual(payload["accounts"], stats)

    def test_monitor_requires_admin_authorization(self):
        response = self.client.get("/api/monitor/realtime")
        self.assertEqual(response.status_code, 401, response.text)

    def test_admin_can_fetch_a_realtime_monitor_detail(self):
        detail = {"call_id": "call-1", "status": "failed", "events": []}
        with mock.patch.object(system_module.realtime_monitor_service, "detail", return_value=detail):
            response = self.client.get(
                "/api/monitor/realtime/call-1",
                headers={"Authorization": f"Bearer {config.auth_key}"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"detail": detail})

    def test_realtime_monitor_detail_returns_not_found(self):
        with mock.patch.object(system_module.realtime_monitor_service, "detail", return_value={}):
            response = self.client.get(
                "/api/monitor/realtime/missing",
                headers={"Authorization": f"Bearer {config.auth_key}"},
            )

        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
