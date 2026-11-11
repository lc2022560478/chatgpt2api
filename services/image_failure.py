from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        result: list[str] = []
        for key in ("code", "type", "message", "detail", "error"):
            result.extend(_walk_strings(value.get(key)))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_walk_strings(item))
        return result
    return []


def is_image_quota_error_text(value: Any) -> bool:
    """Return whether upstream text describes an exhausted image quota."""
    lower = "\n".join(_walk_strings(value)).lower()
    return bool(
        ("no available" in lower and "image quota" in lower)
        or "image generation request limit" in lower
        or "image generation requests limit" in lower
        or "image generation limit" in lower
        or "image generation quota" in lower
        or any(marker in lower for marker in (
            "图像生成请求上限", "图片生成请求上限", "图像生成上限", "图片生成上限",
            "圖像生成請求上限", "圖片生成請求上限", "圖像生成上限", "圖片生成上限",
            "图像生成额度", "图片生成额度", "圖像生成額度", "圖片生成額度",
        ))
        or ("quota" in lower and any(marker in lower for marker in ("image", "图片", "图像", "圖片", "圖像")))
    )


_QUOTA_DURATION_PART = r"\d+(?:\.\d+)?\s*(?:天|小时|小時|分鐘|分钟|秒|days?|hours?|minutes?|seconds?)"
_QUOTA_DURATION_EXPRESSION = rf"{_QUOTA_DURATION_PART}(?:\s*(?:和|与|及|and|,|，|、)?\s*{_QUOTA_DURATION_PART})*"
_QUOTA_DURATION_RE = re.compile(
    rf"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>天|小时|小時|分鐘|分钟|秒|days?|hours?|minutes?|seconds?)",
    re.IGNORECASE,
)
_QUOTA_RESET_PATTERNS = (
    re.compile(
        rf"(?:reset|resets|resetting|renew|renewed).{{0,40}}?\b(?:in|after)\s+(?P<duration>{_QUOTA_DURATION_EXPRESSION})",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        rf"\bin\s+(?P<duration>{_QUOTA_DURATION_EXPRESSION}).{{0,30}}?\b(?:reset|renew|available|create|生成)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        rf"(?:上限|额度|限制).{{0,45}}?(?:将在|会在|在|將在|會在)\s*(?P<duration>{_QUOTA_DURATION_EXPRESSION})\s*(?:之后|后|後)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        rf"(?:将在|会在|在|將在|會在)\s*(?P<duration>{_QUOTA_DURATION_EXPRESSION})\s*(?:之后|后|後).{{0,30}}?(?:重置|恢复|生成|创建|重設|恢復)",
        re.IGNORECASE | re.DOTALL,
    ),
)
_QUOTA_UNIT_SECONDS = {
    "秒": 1.0,
    "分钟": 60.0,
    "分鐘": 60.0,
    "小时": 3600.0,
    "小時": 3600.0,
    "天": 86400.0,
    "second": 1.0,
    "seconds": 1.0,
    "minute": 60.0,
    "minutes": 60.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "day": 86400.0,
    "days": 86400.0,
}


def _quota_reset_delay(value: Any) -> timedelta | None:
    text = "\n".join(_walk_strings(value))
    if not text:
        return None
    match = None
    for pattern in _QUOTA_RESET_PATTERNS:
        match = pattern.search(text)
        if match:
            break
    if match is None:
        return None
    parts = list(_QUOTA_DURATION_RE.finditer(match.group("duration")))
    if not parts:
        return None
    total_seconds = 0.0
    for part in parts:
        try:
            amount = float(part.group("value"))
        except (TypeError, ValueError):
            return None
        unit_seconds = _QUOTA_UNIT_SECONDS.get(part.group("unit").lower())
        if unit_seconds is None:
            return None
        total_seconds += amount * unit_seconds
    try:
        return timedelta(seconds=total_seconds) if total_seconds >= 0 else None
    except OverflowError:
        return None


def extract_image_quota_restore_at(value: Any, *, now: datetime | None = None) -> str | None:
    """Convert an upstream image-quota countdown into an absolute UTC time."""
    delay = _quota_reset_delay(value)
    if delay is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        return (current.astimezone(timezone.utc) + delay).isoformat()
    except OverflowError:
        return None


