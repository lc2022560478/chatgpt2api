from __future__ import annotations

import math
import os
import time
from collections import Counter, deque
from datetime import datetime
from threading import Lock
from typing import Any


def _timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(value if value is not None else time.time()).astimezone().isoformat(timespec="seconds")


def _text(value: object, limit: int = 240) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _number(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _millis(value: object) -> int:
    return max(0, _number(value))


def _env_int(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    value = _number(os.getenv(name), default)
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


STAGE_LABELS = {
    "handler_submitted": "等待入口",
    "handler_started": "入口执行",
    "stream_first_item": "读取首包",
    "queued": "排队中",
    "running": "任务执行中",
    "getting_account": "分配账号",
    "acquiring_account": "分配账号",
    "account_ready": "账号就绪",
    "account_selected": "账号已分配",
    "uploading": "上传参考图",
    "bootstrapping": "初始化上游会话",
    "getting_token": "获取上游令牌",
    "preparing_conversation": "准备生图会话",
    "starting_generation": "提交生图请求",
    "generating": "上游生成中",
    "image_stream_resolve_start": "等待图片结果",
    "resolving": "解析结果",
    "resolving_image": "解析图片结果",
    "receiving_image": "接收图片结果",
    "image_ready": "图片已就绪",
    "upstream_message": "接收上游文本回复",
    "retrying_account": "切换账号重试",
    "account_retries_exhausted": "账号重试已耗尽",
    "request_timeout": "请求总超时",
    "rejected": "请求被拒绝",
    "completed": "已完成",
    "failed": "失败",
}


METRIC_LABELS = {
    "handler_queue_ms": "等待入口",
    "handler_exec_ms": "入口执行",
    "stream_first_queue_ms": "首包线程等待",
    "stream_first_exec_ms": "首包读取",
    "account_wait_ms": "等待账号",
    "upload_ms": "图片上传",
    "bootstrap_ms": "上游初始化",
    "requirements_ms": "获取令牌",
    "prepare_conversation_ms": "准备会话",
    "generation_start_ms": "提交生图",
    "sse_stream_ms": "上游流耗时",
    "poll_wait_ms": "等待图片结果",
    "poll_request_ms": "查询图片结果",
    "resolve_ms": "结果处理",
    "download_ms": "图片下载",
    "stream_ms": "单图生成流",
    "total_ms": "单图总耗时",
}


_DETAIL_FIELDS = (
    "account_email",
    "previous_account_email",
    "conversation_id",
    "code",
    "failure_code",
    "error_type",
    "status_code",
    "url_count",
)

_FAILURE_FIELDS = ("error", "code", "failure_code", "error_type", "status_code")


def _clear_failure(record: dict[str, Any]) -> None:
    for key in _FAILURE_FIELDS:
        record.pop(key, None)


class RealtimeMonitorService:
    """Bounded, in-memory lifecycle data for the administrator monitor."""

    def __init__(self, recent_capacity: int | None = None, event_capacity: int | None = None) -> None:
        completed_limit = recent_capacity or _env_int("CHATGPT2API_MONITOR_COMPLETED_LIMIT", 500, 50, 5000)
        events_limit = event_capacity or _env_int("CHATGPT2API_MONITOR_EVENT_LIMIT", 1000, 100, 10000)
        self._lock = Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=completed_limit)
        self._events: deque[dict[str, Any]] = deque(maxlen=events_limit)
        self._finished_call_ids: deque[str] = deque(maxlen=completed_limit)
        self._finished_call_id_set: set[str] = set()
        self._threadpool = {
            "tokens": _env_int("CHATGPT2API_THREAD_TOKENS", 80, 1),
            "previous_tokens": 0,
        }

    def set_threadpool(self, *, tokens: int, previous_tokens: int = 0) -> None:
        with self._lock:
            self._threadpool = {
                "tokens": _millis(tokens),
                "previous_tokens": _millis(previous_tokens),
            }

    def _remember_finished_locked(self, call_id: str) -> None:
        if call_id in self._finished_call_id_set:
            return
        if self._finished_call_ids.maxlen and len(self._finished_call_ids) >= self._finished_call_ids.maxlen:
            self._finished_call_id_set.discard(self._finished_call_ids.popleft())
        self._finished_call_ids.append(call_id)
        self._finished_call_id_set.add(call_id)

    def _record(
        self,
        call_id: str,
        *,
        endpoint: object = "",
        model: object = "",
        summary: object = "",
        role: object = "",
        key_name: object = "",
        stage: str = "handler_submitted",
    ) -> dict[str, Any]:
        now_wall = time.time()
        now_monotonic = time.monotonic()
        return {
            "call_id": call_id,
            "endpoint": _text(endpoint, 120),
            "model": _text(model, 120),
            "summary": _text(summary, 160),
            "role": _text(role, 40),
            "key_name": _text(key_name, 120),
            "status": "running",
            "stage": stage,
            "stage_label": STAGE_LABELS.get(stage, stage),
            "started_at": _timestamp(now_wall),
            "updated_at": _timestamp(now_wall),
            "metrics": {},
            "perf": {},
            "images": {},
            "retry_count": 0,
            "_started_monotonic": now_monotonic,
            "_stage_started_monotonic": now_monotonic,
        }

    def start(
        self,
        call_id: str,
        *,
        endpoint: str,
        model: str,
        summary: str,
        role: object = "",
        key_name: object = "",
    ) -> None:
        call_id = str(call_id or "").strip()
        if not call_id:
            return
        with self._lock:
            if call_id in self._active or call_id in self._finished_call_id_set:
                return
            record = self._record(
                call_id,
                endpoint=endpoint,
                model=model,
                summary=summary,
                role=role,
                key_name=key_name,
            )
            self._active[call_id] = record
            self._append_event_locked(record, "handler_submitted")

    def stage(self, call_id: str, stage: str, **details: object) -> None:
        call_id = str(call_id or "").strip()
        stage = _text(stage, 80) or "running"
        if not call_id:
            return
        with self._lock:
            if call_id in self._finished_call_id_set:
                return
            record = self._active.get(call_id)
            if record is None:
                record = self._record(
                    call_id,
                    endpoint=details.get("endpoint", ""),
                    model=details.get("model", ""),
                    stage=stage,
                )
                self._active[call_id] = record
            if record.get("stage") != stage:
                record["_stage_started_monotonic"] = time.monotonic()
            record["stage"] = stage
            record["stage_label"] = STAGE_LABELS.get(stage, stage)
            record["updated_at"] = _timestamp()
            self._merge_details(record, details)
            self._append_event_locked(record, stage, details)

    def capture_image_attempts(self, call_id: str, attempts: object) -> None:
        call_id = str(call_id or "").strip()
        if not call_id:
            return
        if not isinstance(attempts, (list, tuple)):
            return
        with self._lock:
            record = self._active.get(call_id)
            if record is None:
                return
            for item in attempts:
                if not isinstance(item, dict):
                    continue
                index = _millis(item.get("slot") or item.get("index"))
                if index <= 0:
                    continue
                image = record.setdefault("images", {}).setdefault(str(index), {"index": index, "metrics": {}})
                for source, target in (
                    ("total", "total"),
                    ("attempt", "account_attempt"),
                    ("max_attempts", "max_account_attempts"),
                    ("max_account_attempts", "max_account_attempts"),
                    ("retry_count", "retry_count"),
                    ("account_switch_count", "account_switch_count"),
                ):
                    if source in item:
                        image[target] = _millis(item.get(source))
                for key in ("status", "account_email", "previous_account_email", "conversation_id", "code", "error"):
                    value = item.get(key)
                    if value not in (None, ""):
                        image[key] = _text(value, 1000 if key == "error" else 240)
                for key in ("returned_result", "returned_message"):
                    if key in item:
                        image[key] = bool(item.get(key))
                if image.get("status") == "success" or image.get("returned_result"):
                    _clear_failure(image)
                metric_values = item.get("metrics")
                if isinstance(metric_values, dict):
                    self._merge_metrics(
                        image.setdefault("metrics", {}),
                        {key: metric_values[key] for key in METRIC_LABELS if key in metric_values},
                    )
                self._merge_metrics(
                    image.setdefault("metrics", {}),
                    {key: item[key] for key in METRIC_LABELS if key in item},
                )
            self._refresh_image_summary(record)

    def finish(self, call_id: str, status: str, **details: object) -> None:
        call_id = str(call_id or "").strip()
        if not call_id:
            return
        with self._lock:
            record = self._active.pop(call_id, None)
            if record is None:
                # A handler can finalize through both its normal and exception paths.
                # Preserve the first terminal result so aggregate metrics stay accurate.
                if call_id in self._finished_call_id_set:
                    return
                record = self._record(
                    call_id,
                    endpoint=details.get("endpoint", ""),
                    model=details.get("model", ""),
                    stage="completed" if status == "success" else "failed",
                )
            normalized_status = "success" if status == "success" else "failed"
            record["status"] = normalized_status
            record["stage"] = "completed" if normalized_status == "success" else "failed"
            record["stage_label"] = STAGE_LABELS[record["stage"]]
            record["updated_at"] = _timestamp()
            record["ended_at"] = record["updated_at"]
            self._merge_details(record, details)
            if normalized_status == "success":
                _clear_failure(record)
            duration = _millis(details.get("duration_ms"))
            if not duration:
                duration = max(0, int((time.monotonic() - float(record["_started_monotonic"])) * 1000))
            record["duration_ms"] = duration
            self._refresh_image_summary(record)
            self._append_event_locked(record, record["stage"], details)
            self._recent.appendleft(self._copy_record(record))
            self._remember_finished_locked(call_id)

    def detail(self, call_id: str) -> dict[str, Any]:
        call_id = str(call_id or "").strip()
        if not call_id:
            return {}
        with self._lock:
            record = self._active.get(call_id)
            if record is None:
                record = next((item for item in self._recent if item.get("call_id") == call_id), None)
            if record is None:
                return {}
            item = self._public_record(record, time.monotonic())
            item["events"] = [dict(event) for event in self._events if event.get("call_id") == call_id][:100]
        return item

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            active = [self._public_record(record, now) for record in self._active.values()]
            recent = [self._public_record(record, now) for record in self._recent]
            events = [dict(item) for item in self._events]
            threadpool = dict(self._threadpool)
            completed_capacity = self._recent.maxlen
            event_capacity = self._events.maxlen

        active.sort(key=lambda item: str(item.get("started_at") or ""))
        slow = sorted(
            recent,
            key=lambda item: max(
                _millis(item.get("duration_ms")),
                self._metric_value(item, "handler_queue_ms") * 4,
                self._metric_value(item, "stream_first_queue_ms") * 4,
                self._metric_value(item, "account_wait_ms") * 2,
            ),
            reverse=True,
        )
        return {
            "updated_at": _timestamp(),
            "threadpool": threadpool,
            "window": {
                "completed": len(recent),
                "completed_capacity": completed_capacity,
                "events": len(events),
                "event_capacity": event_capacity,
            },
            "summary": self._summary(active, recent),
            "active": active[:100],
            "recent": recent[:80],
            "slow": slow[:50],
            "events": events[:80],
            "metric_labels": dict(METRIC_LABELS),
        }

    def _merge_details(self, record: dict[str, Any], details: dict[str, object]) -> None:
        for key in ("endpoint", "model", "summary", "role", "key_name"):
            if details.get(key) not in (None, ""):
                record[key] = _text(details[key], 160 if key == "summary" else 120)
        for key in _DETAIL_FIELDS:
            if details.get(key) not in (None, ""):
                record[key] = _text(details[key], 1000 if key == "error" else 240)
        if details.get("error") not in (None, ""):
            record["error"] = _text(details["error"], 1000)
        for key in ("attempt", "max_attempts", "retry_count", "index", "total", "account_switch_count"):
            if key in details:
                record[key] = _millis(details[key])
        if record.get("stage") == "retrying_account":
            record["retry_count"] = max(_millis(record.get("retry_count")), _millis(details.get("retry_count", 1)))
        self._merge_metrics(record.setdefault("metrics", {}), details)
        self._merge_metrics(record.setdefault("perf", {}), details.get("perf"))

        index = _millis(details.get("index"))
        if index > 0:
            images = record.setdefault("images", {})
            image = images.setdefault(str(index), {"index": index, "metrics": {}})
            image["stage"] = record.get("stage", "")
            image["stage_label"] = record.get("stage_label", "")
            image["updated_at"] = record.get("updated_at", "")
            for key in (
                "total",
                "attempt",
                "max_attempts",
                "retry_count",
                "account_email",
                "previous_account_email",
                "conversation_id",
                "status",
                "error",
                "code",
            ):
                if key in details and details[key] not in (None, ""):
                    image_key = "account_attempt" if key == "attempt" else "max_account_attempts" if key == "max_attempts" else key
                    image[image_key] = _text(details[key], 1000 if key == "error" else 240) if key in {
                        "account_email", "previous_account_email", "conversation_id", "error", "code"
                    } else details[key]
            self._merge_metrics(image.setdefault("metrics", {}), details)
            if record.get("stage") == "image_ready":
                image["status"] = "success"
                _clear_failure(image)
            self._refresh_image_summary(record)

    @staticmethod
    def _merge_metrics(target: dict[str, int], values: object) -> None:
        if not isinstance(values, dict):
            return
        for key, value in values.items():
            if str(key).endswith("_ms"):
                target[str(key)] = max(_millis(target.get(str(key))), _millis(value))

    @staticmethod
    def _refresh_image_summary(record: dict[str, Any]) -> None:
        images = record.get("images")
        if not isinstance(images, dict):
            return
        values = [item for item in images.values() if isinstance(item, dict)]
        if not values:
            return
        record["image_account_attempt"] = max((_millis(item.get("account_attempt")) for item in values), default=0)
        record["image_account_max_attempts"] = max((_millis(item.get("max_account_attempts")) for item in values), default=0)
        record["image_account_switch_count"] = sum(_millis(item.get("retry_count")) for item in values)

    def _append_event_locked(self, record: dict[str, Any], event: str, details: dict[str, object] | None = None) -> None:
        payload: dict[str, Any] = {
            "time": _timestamp(),
            "call_id": record["call_id"],
            "event": event,
            "label": STAGE_LABELS.get(event, event),
            "endpoint": record.get("endpoint", ""),
            "model": record.get("model", ""),
            "stage": record.get("stage", ""),
        }
        source = details or {}
        for key in (*_DETAIL_FIELDS, "error", "attempt", "max_attempts", "retry_count", "index", "total", "account_switch_count"):
            if key in _FAILURE_FIELDS:
                # Errors describe this event, not every later stage of the request.
                # Keep the original retry event for diagnostics after recovery.
                if event in {"image_ready", "completed"}:
                    continue
                value = source.get(key, record.get(key) if event == "failed" else None)
            else:
                value = source.get(key, record.get(key))
            if value not in (None, "", 0):
                payload[key] = _text(value, 1000 if key == "error" else 240) if key in {
                    "account_email", "previous_account_email", "conversation_id", "error", "code"
                } else value
        for key, value in source.items():
            if str(key).endswith("_ms") and _millis(value) > 0:
                payload[str(key)] = _millis(value)
        self._events.appendleft(payload)

    def _public_record(self, record: dict[str, Any], now: float) -> dict[str, Any]:
        item = self._copy_record(record)
        started = float(record.get("_started_monotonic") or now)
        stage_started = float(record.get("_stage_started_monotonic") or started)
        if item.get("status") == "running":
            item["elapsed_ms"] = max(0, int((now - started) * 1000))
            item["stage_elapsed_ms"] = max(0, int((now - stage_started) * 1000))
        else:
            item["elapsed_ms"] = _millis(item.get("duration_ms"))
            item["stage_elapsed_ms"] = 0
        item.pop("_started_monotonic", None)
        item.pop("_stage_started_monotonic", None)
        return item

    @staticmethod
    def _copy_record(record: dict[str, Any]) -> dict[str, Any]:
        item = dict(record)
        item["metrics"] = dict(record.get("metrics") or {})
        item["perf"] = dict(record.get("perf") or {})
        images = record.get("images") if isinstance(record.get("images"), dict) else {}
        item["images"] = {
            str(key): {**value, "metrics": dict(value.get("metrics") or {})}
            for key, value in images.items()
            if isinstance(value, dict)
        }
        return item

    def _summary(self, active: list[dict[str, Any]], recent: list[dict[str, Any]]) -> dict[str, Any]:
        success = sum(1 for item in recent if item.get("status") == "success")
        failed = sum(1 for item in recent if item.get("status") == "failed")
        switches = sum(max(_millis(item.get("retry_count")), _millis(item.get("image_account_switch_count"))) for item in recent)
        switch_requests = sum(
            1 for item in recent if max(_millis(item.get("retry_count")), _millis(item.get("image_account_switch_count"))) > 0
        )
        switch_success = sum(
            1
            for item in recent
            if item.get("status") == "success"
            and max(_millis(item.get("retry_count")), _millis(item.get("image_account_switch_count"))) > 0
        )
        durations = [_millis(item.get("duration_ms")) for item in recent if _millis(item.get("duration_ms"))]
        metric_p95 = {key: self._percentile(self._metric_values(recent, key), 95) for key in METRIC_LABELS}
        active_stages = Counter(str(item.get("stage_label") or item.get("stage") or "运行中") for item in active)
        return {
            "active": len(active),
            "completed": len(recent),
            "success": success,
            "failed": failed,
            "success_rate": round(success * 100 / (success + failed), 1) if success + failed else 0.0,
            "account_switches": switches,
            "account_switch_requests": switch_requests,
            "account_switch_success": switch_success,
            "account_switch_recovery_rate": round(switch_success * 100 / switch_requests, 1) if switch_requests else 0.0,
            "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
            "p95_duration_ms": self._percentile(durations, 95),
            "metric_p95": metric_p95,
            "slow_counts": {
                "handler_queue": sum(1 for item in recent if self._metric_value(item, "handler_queue_ms") >= 1000),
                "stream_first_queue": sum(1 for item in recent if self._metric_value(item, "stream_first_queue_ms") >= 1000),
                "account_wait": sum(1 for item in recent if self._metric_value(item, "account_wait_ms") >= 5000),
                "total_over_120s": sum(1 for item in recent if _millis(item.get("duration_ms")) >= 120000),
            },
            "by_model": dict(Counter(str(item.get("model") or "unknown") for item in recent).most_common(10)),
            "active_by_model": dict(Counter(str(item.get("model") or "unknown") for item in active).most_common(10)),
            "active_by_stage": dict(active_stages.most_common(10)),
        }

    def _metric_values(self, records: list[dict[str, Any]], key: str) -> list[int]:
        return [value for value in (self._metric_value(record, key) for record in records) if value > 0]

    @staticmethod
    def _metric_value(record: dict[str, Any], key: str) -> int:
        metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
        perf = record.get("perf") if isinstance(record.get("perf"), dict) else {}
        return max(_millis(metrics.get(key)), _millis(perf.get(key)))

    @staticmethod
    def _percentile(values: list[int], percentile: int) -> int:
        ordered = sorted(value for value in values if value > 0)
        if not ordered:
            return 0
        index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile / 100) - 1))
        return ordered[index]


realtime_monitor_service = RealtimeMonitorService()
