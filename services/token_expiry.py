from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any


_EXPIRY_FIELDS = {
    "access": ("access_token_expires_at", "expires_at", "expired", "expires"),
    "refresh": ("refresh_token_expires_at", "refresh_expires_at"),
}
_LIFETIME_FIELDS = {
    "access": ("access_token_expires_in", "expires_in"),
    "refresh": ("refresh_token_expires_in", "refresh_expires_in"),
}


def parse_expiry_time(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    if seconds > 10_000_000_000:
        seconds /= 1000
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _jwt_expiry(token: str) -> datetime | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        value = claims.get("exp") if isinstance(claims, dict) else None
        # JWT exp is a NumericDate, not an ISO date or a millisecond timestamp.
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0 or seconds >= 253_402_300_800:
            return None
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError, UnicodeError):
        return None


def token_expiry_fields(
    payload: dict[str, Any], *, received_at: datetime | None = None
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for kind in ("access", "refresh"):
        token = payload.get(f"{kind}_token") or (payload.get("accessToken") if kind == "access" else "")
        expiry = _jwt_expiry(token) if isinstance(token, str) and token.strip() else None
        if token:
            for field in _EXPIRY_FIELDS[kind]:
                if expiry is not None:
                    break
                expiry = parse_expiry_time(payload.get(field))
            # Relative lifetimes are converted only when receiving credentials,
            # never on account reads or restart (which would extend their life).
            if expiry is None and received_at is not None:
                for field in _LIFETIME_FIELDS[kind]:
                    value = payload.get(field)
                    if value is None or isinstance(value, bool):
                        continue
                    try:
                        lifetime = float(value)
                        if math.isfinite(lifetime) and lifetime >= 0:
                            expiry = received_at + timedelta(seconds=lifetime)
                            break
                    except (ValueError, TypeError, OverflowError):
                        continue
        result[f"{kind}_token_expires_at"] = expiry.isoformat() if expiry is not None else None
    return result


def merge_account_credentials(
    current: dict[str, Any], updates: dict[str, Any], *, received_at: datetime | None = None
) -> dict[str, Any]:
    result = dict(current)
    for kind in ("access", "refresh"):
        field = f"{kind}_token"
        token_changed = field in updates and updates[field] != current.get(field)
        expiry_supplied = any(name in updates for name in (*_EXPIRY_FIELDS[kind], *_LIFETIME_FIELDS[kind]))
        if token_changed or expiry_supplied:
            for name in (*_EXPIRY_FIELDS[kind], *_LIFETIME_FIELDS[kind]):
                result.pop(name, None)
    result.update(updates)
    expiry_payload = dict(result)
    for fields in _LIFETIME_FIELDS.values():
        for field in fields:
            if field not in updates:
                expiry_payload.pop(field, None)
    result.update(token_expiry_fields(expiry_payload, received_at=received_at))
    for fields in _LIFETIME_FIELDS.values():
        for field in fields:
            result.pop(field, None)
    return result
