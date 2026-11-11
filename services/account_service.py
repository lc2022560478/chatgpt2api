from __future__ import annotations

import base64
import json
import re
import secrets
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, Lock, Thread
from typing import Any
from urllib.parse import urlencode

from services.config import config
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.storage.base import StorageBackend
from services.token_expiry import merge_account_credentials, parse_expiry_time, token_expiry_fields
from utils.helper import anonymize_token


_IMAGE_PREFLIGHT_TRANSIENT_CURL_ERROR_RE = re.compile(
    r"\bcurl\s*:\s*\(\s*(?:5|6|7|18|28|35|52|55|56|92)\s*\)",
    re.IGNORECASE,
)
_IMAGE_PREFLIGHT_TIMEOUT_CURL_ERROR_RE = re.compile(
    r"\bcurl\s*:\s*\(\s*28\s*\)",
    re.IGNORECASE,
)


def _is_image_preflight_timeout(error: object) -> bool:
    if isinstance(error, TimeoutError) or bool(getattr(error, "is_timeout", False)):
        return True
    raw_error = str(getattr(error, "raw_error", "") or error or "")
    return bool(_IMAGE_PREFLIGHT_TIMEOUT_CURL_ERROR_RE.search(raw_error)) or any(
        marker in raw_error.lower() for marker in ("timed out", "timeout", "time out")
    )


class ImagePreflightTransportError(RuntimeError):
    """A transient connection failure while validating an image account.

    This is intentionally distinct from an invalid account or exhausted quota:
    callers should retry the image request instead of immediately probing more
    account tokens. ``raw_error`` keeps the upstream transport diagnostic for
    server-side retry and logging decisions.
    """

    def __init__(self, raw_error: object, *, is_timeout: bool | None = None) -> None:
        self.is_timeout = _is_image_preflight_timeout(raw_error) if is_timeout is None else bool(is_timeout)
        self.raw_error = str(getattr(raw_error, "raw_error", "") or raw_error or "").strip()
        super().__init__(self.raw_error or "image account preflight transport failed")


class _ImageQuotaUnavailableError(RuntimeError):
    """No eligible image account remains in the local candidate pool."""


def is_transient_image_preflight_transport_error(error: BaseException | str) -> bool:
    """Identify only connection failures that say nothing about account health."""
    if isinstance(error, ImagePreflightTransportError):
        return True
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    raw_error = str(getattr(error, "raw_error", "") or error or "")
    lower = raw_error.lower()
    return _is_image_preflight_timeout(error) or bool(_IMAGE_PREFLIGHT_TRANSIENT_CURL_ERROR_RE.search(raw_error)) or any(
        marker in lower
        for marker in (
            "connection reset",
            "connection aborted",
            "connection abort",
            "connection refused",
            "connection closed",
            "connection lost",
            "failed to connect",
            "could not connect",
            "could not resolve",
            "couldn't resolve",
            "name or service not known",
            "temporary failure in name resolution",
            "network is unreachable",
            "no route to host",
            "broken pipe",
            "tls connect error",
            "tls handshake",
            "ssl connect error",
            "ssl connection",
            "ssl_error_syscall",
            "empty reply from server",
            "recv failure",
            "send failure",
        )
    )


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    _NEW_ACCOUNT_INVALID_GRACE_SECONDS = 10 * 60
    _INVALID_CONFIRM_SECONDS = 30
    _ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 24 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_SECONDS = 3 * 24 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_ERROR_BACKOFF_SECONDS = 6 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_BATCH_SIZE = 3
    _TOKEN_REFRESH_ERROR_BACKOFF_SECONDS = 5 * 60
    # Remote account-info validation is expensive and a burst of image tasks can
    # otherwise repeat it for the same healthy account. This is deliberately a
    # short, in-memory-only proof of a successful preflight, not an account-data
    # cache: quota and status always come from the current local account record.
    _IMAGE_PREFLIGHT_CACHE_TTL_SECONDS = 30.0
    _OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
    _OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
    _OAUTH_REDIRECT_URI = "com.openai.chat://auth0.openai.com/ios/com.openai.chat/callback"
    _OAUTH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 刷新进度追踪
    _refresh_progress: dict[str, dict] = {}
    _refresh_progress_lock = Lock()
    # 重新登录进度追踪
    _relogin_progress: dict[str, dict] = {}
    _relogin_progress_lock = Lock()

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = Lock()
        self._token_refresh_lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}
        self._token_aliases: dict[str, str] = {}
        self._image_preflight_cache: dict[str, float] = {}
        self._image_preflight_flights: dict[str, Future] = {}
        self._cumulative_total = self._load_cumulative_total()

    def _get_cumulative_file(self) -> Path:
        from services.config import DATA_DIR
        return DATA_DIR / ".cumulative_total"

    def _load_cumulative_total(self) -> int:
        try:
            f = self._get_cumulative_file()
            if f.exists():
                return int(f.read_text().strip())
        except Exception:
            pass
        return len(self._accounts)

    def _save_cumulative_total(self) -> None:
        try:
            self._get_cumulative_file().write_text(str(self._cumulative_total))
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        try:
            payload = str(token or "").split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            import base64
            import json
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _timestamp_to_iso(value: object) -> str:
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return ""
        tz = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).isoformat()

    def _load_accounts(self) -> dict[str, dict]:
        accounts = self.storage.load_accounts()
        return {
            normalized["access_token"]: normalized
            for item in accounts
            if (normalized := self._normalize_account(item)) is not None
        }

    def _save_accounts(self) -> None:
        self.storage.save_accounts(list(self._accounts.values()))

    @staticmethod
    def _is_image_account_available(account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "限流", "异常"}:
            return False
        now = datetime.now(timezone.utc)
        for field in (
            "image_success_cooldown_until",
            "image_quota_cooldown_until",
            "image_model_not_found_cooldown_until",
        ):
            cooldown_until = AccountService._parse_time(account.get(field))
            if cooldown_until is not None and cooldown_until > now:
                return False
        return int(account.get("quota") or 0) > 0

    @classmethod
    def _account_matches_plan_type(cls, account: dict, plan_type: str | None = None) -> bool:
        if not plan_type:
            return True
        normalized_plan = cls._normalize_account_type(plan_type)
        normalized_account = cls._normalize_account_type(account.get("type"))
        if not normalized_plan or not normalized_account:
            return False
        return normalized_plan.lower() == normalized_account.lower()

    @classmethod
    def _account_matches_source_type(cls, account: dict, source_type: str | None = None) -> bool:
        if not source_type:
            return True
        return cls._normalize_source_type(account.get("source_type")) == cls._normalize_source_type(source_type)

    @classmethod
    def _account_matches_any_plan_type(cls, account: dict, plan_types: set[str] | tuple[str, ...] | None = None) -> bool:
        if not plan_types:
            return True
        normalized_account = cls._normalize_account_type(account.get("type"))
        normalized_plans = {
            normalized
            for plan_type in plan_types
            if (normalized := cls._normalize_account_type(plan_type))
        }
        return bool(normalized_account and normalized_account in normalized_plans)

    @staticmethod
    def _normalize_source_type(value: object) -> str:
        return str(value or "web").strip().lower() or "web"

    @staticmethod
    def _normalize_account_type(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        key = raw.lower().replace("-", "_").replace(" ", "_")
        compact = key.replace("_", "")
        aliases = {
            "free": "free",
            "plus": "Plus",
            "pro": "Pro",
            "prolite": "ProLite",
            "team": "Team",
            "business": "Team",
            "enterprise": "Enterprise",
        }
        return aliases.get(compact) or aliases.get(key) or raw

    def _search_account_type(self, payload: object) -> str | None:
        if isinstance(payload, dict):
            for key in ("plan_type", "account_plan", "account_type", "subscription_type", "type"):
                plan = self._normalize_account_type(payload.get(key))
                if plan:
                    return plan
            for value in payload.values():
                plan = self._search_account_type(value)
                if plan:
                    return plan
        elif isinstance(payload, list):
            for value in payload:
                plan = self._search_account_type(value)
                if plan:
                    return plan
        return None

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or item.get("accessToken") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized.pop("accessToken", None)
        normalized["access_token"] = access_token
        normalized.update(token_expiry_fields(normalized))
        if str(normalized.get("type") or "").strip().lower() == "codex":
            normalized["export_type"] = "codex"
            normalized.pop("type", None)
        normalized["type"] = normalized.get("type") or "free"
        normalized["status"] = normalized.get("status") or "正常"
        normalized["quota"] = max(0, int(normalized.get("quota") if normalized.get("quota") is not None else 0))
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        normalized["proxy"] = str(normalized.get("proxy") or "").strip()
        source_type = normalized.get("source_type")
        if not source_type and str(normalized.get("export_type") or "").strip().lower() == "codex":
            source_type = "codex"
        normalized["source_type"] = self._normalize_source_type(source_type)
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["image_success_cooldown_until"] = normalized.get("image_success_cooldown_until") or None
        normalized["image_quota_cooldown_until"] = normalized.get("image_quota_cooldown_until") or None
        normalized["image_model_not_found_cooldown_until"] = normalized.get("image_model_not_found_cooldown_until") or None
        normalized["image_quota_limited"] = bool(normalized.get("image_quota_limited"))
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["invalid_count"] = int(normalized.get("invalid_count") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        normalized["last_invalid_at"] = normalized.get("last_invalid_at") or None
        normalized["last_refresh_error"] = normalized.get("last_refresh_error") or None
        normalized["last_refresh_error_at"] = normalized.get("last_refresh_error_at") or None
        normalized["last_token_refresh_at"] = normalized.get("last_token_refresh_at") or None
        normalized["last_token_refresh_error"] = normalized.get("last_token_refresh_error") or None
        normalized["last_token_refresh_error_at"] = normalized.get("last_token_refresh_error_at") or None
        normalized["created_at"] = normalized.get("created_at") or AccountService._now()
        return normalized

    @staticmethod
    def _jwt_exp(access_token: str) -> int:
        try:
            return int(AccountService._decode_jwt_payload(access_token).get("exp") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _token_expires_in(cls, access_token: str, expires_at: object = None) -> int | None:
        expiry = parse_expiry_time(token_expiry_fields({
            "access_token": access_token, "access_token_expires_at": expires_at,
        })["access_token_expires_at"])
        if expiry is None:
            return None
        return int(expiry.timestamp() - time.time())

    @classmethod
    def _token_needs_refresh(cls, access_token: str, *, force: bool = False, expires_at: object = None) -> bool:
        if force:
            return True
        remaining = cls._token_expires_in(access_token, expires_at)
        return remaining is not None and remaining <= cls._ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @classmethod
    def _token_issued_at(cls, access_token: str) -> datetime | None:
        try:
            iat = int(cls._decode_jwt_payload(access_token).get("iat") or 0)
        except (TypeError, ValueError):
            return None
        if iat <= 0:
            return None
        return datetime.fromtimestamp(iat, tz=timezone.utc)

    @staticmethod
    def _safe_response_text(response: object, limit: int = 300) -> str:
        try:
            return str(getattr(response, "text", "") or "")[:limit]
        except Exception:
            return ""

    def _resolve_access_token_locked(self, access_token: str) -> str:
        token = str(access_token or "").strip()
        seen: set[str] = set()
        while token and token not in self._accounts and token in self._token_aliases and token not in seen:
            seen.add(token)
            token = self._token_aliases.get(token, token)
        return token

    def resolve_access_token(self, access_token: str) -> str:
        if not access_token:
            return ""
        with self._lock:
            return self._resolve_access_token_locked(access_token)

    def _invalidate_image_preflight_cache_locked(self, *access_tokens: str) -> None:
        """Drop in-memory preflight proofs for the supplied account tokens.

        Callers hold ``_lock`` (or ``_image_slot_condition``, which uses the
        same lock). Cache entries intentionally use raw access tokens as keys,
        but only in memory and only for their short TTL.
        """
        for raw_token in access_tokens:
            token = str(raw_token or "").strip()
            if not token:
                continue
            self._image_preflight_cache.pop(token, None)
            self._image_preflight_cache.pop(self._resolve_access_token_locked(token), None)

    def _get_cached_image_preflight_locked(self, access_token: str) -> dict | None:
        """Return a still-safe local account snapshot for a verified token."""
        resolved_token = self._resolve_access_token_locked(access_token)
        expires_at = self._image_preflight_cache.get(resolved_token)
        if expires_at is None:
            return None
        if expires_at <= time.monotonic():
            self._image_preflight_cache.pop(resolved_token, None)
            return None

        # Never use a preflight proof to skip a necessary refresh. Likewise,
        # status/quota are always read from the live local record, rather than
        # from the value returned by the old /me request.
        account = self._accounts.get(resolved_token)
        if self._token_needs_refresh(resolved_token, expires_at=(account or {}).get("access_token_expires_at")):
            self._image_preflight_cache.pop(resolved_token, None)
            return None
        if not self._is_image_account_available(account or {}):
            self._image_preflight_cache.pop(resolved_token, None)
            return None
        return dict(account)

    def _preflight_image_account(
            self,
            access_token: str,
            *,
            event: str,
            timeout_secs: float | None = None,
    ) -> dict[str, Any] | None:
        """Validate an image account once, then share a short-lived success.

        A flight coalesces simultaneous validations of one token. Only a
        completed, healthy validation is cached; exceptions and unavailable
        account results remain uncached so regular invalid-token handling is
        never hidden by this optimization.
        """
        deadline = (
            time.monotonic() + max(0.0, float(timeout_secs))
            if timeout_secs is not None
            else None
        )

        def remaining() -> float | None:
            if deadline is None:
                return None
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("timed out validating image account")
            return value

        with self._lock:
            cached = self._get_cached_image_preflight_locked(access_token)
            if cached is not None:
                return cached
            flight_token = self._resolve_access_token_locked(access_token)
            flight = self._image_preflight_flights.get(flight_token)
            is_leader = flight is None
            if is_leader:
                flight = Future()
                self._image_preflight_flights[flight_token] = flight

        assert flight is not None
        if not is_leader:
            try:
                result = flight.result(timeout=remaining())
            except FutureTimeoutError as exc:
                raise ImagePreflightTransportError(
                    "timed out validating image account", is_timeout=True
                ) from exc
            if not isinstance(result, dict):
                return None
            # A leading validation may rotate or remove the token. Return only
            # the current canonical account, never the stale flight snapshot.
            with self._lock:
                result_token = self._resolve_access_token_locked(
                    str(result.get("access_token") or flight_token)
                )
                current = self._accounts.get(result_token)
                return dict(current) if current is not None else None

        try:
            result = self.fetch_remote_info(
                access_token,
                event,
                timeout_secs=remaining(),
            )
            with self._lock:
                # Do not resolve an old response token through an alias before
                # caching it. A parallel refresh can rotate the account after
                # the remote request began; that old response is not evidence
                # that the replacement token has passed preflight.
                result_token = str((result or {}).get("access_token") or flight_token).strip()
                current = self._accounts.get(result_token)
                public_result = (
                    dict(current)
                    if current is not None
                    else self._accounts.get(self._resolve_access_token_locked(result_token))
                )
                if isinstance(public_result, dict):
                    public_result = dict(public_result)
                if (
                        current is not None
                        and self._is_image_account_available(current)
                        and not self._token_needs_refresh(result_token, expires_at=current.get("access_token_expires_at"))
                ):
                    self._image_preflight_cache[result_token] = (
                        time.monotonic() + self._IMAGE_PREFLIGHT_CACHE_TTL_SECONDS
                    )
                flight.set_result(public_result)
            return public_result
        except BaseException as exc:
            # A transport failure says nothing about the selected account. Its
            # callers must not turn one broken upstream connection into a scan
            # of twenty otherwise healthy account tokens.
            error: BaseException = exc
            if not isinstance(exc, ImagePreflightTransportError) and is_transient_image_preflight_transport_error(exc):
                error = ImagePreflightTransportError(exc)
            # Failures are shared with existing waiters but are never retained
            # in the TTL cache.
            flight.set_exception(error)
            if error is exc:
                raise
            raise error from exc
        finally:
            with self._lock:
                if self._image_preflight_flights.get(flight_token) is flight:
                    self._image_preflight_flights.pop(flight_token, None)

    def _get_account_for_token(self, access_token: str) -> tuple[str, dict | None]:
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(resolved)
            return resolved, dict(account) if account else None

    def _record_token_refresh_error(self, access_token: str, event: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(resolved)
            if current is None:
                return
            error = self._sanitize_token_refresh_error(
                error, access_token, current.get("access_token"), current.get("refresh_token"), current.get("id_token")
            )
            next_item = dict(current)
            next_item["last_token_refresh_error"] = str(error or "refresh token failed")
            next_item["last_token_refresh_error_at"] = now
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[resolved] = account
                self._save_accounts()
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 刷新 access_token 失败",
            {"source": event, "token": anonymize_token(access_token), "error": str(error or "")},
        )

    def _recent_token_refresh_error(self, account: dict) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (datetime.now(timezone.utc) - last_error_at).total_seconds() < self._TOKEN_REFRESH_ERROR_BACKOFF_SECONDS

    def _recent_refresh_token_keepalive_error(self, account: dict, now: datetime) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (now - last_error_at).total_seconds() < self._REFRESH_TOKEN_KEEPALIVE_ERROR_BACKOFF_SECONDS

    def _refresh_token_keepalive_anchor(self, account: dict) -> datetime | None:
        return (
            self._parse_time(account.get("last_token_refresh_at"))
            or self._token_issued_at(str(account.get("access_token") or ""))
            or self._parse_time(account.get("created_at"))
        )

    def _refresh_token_keepalive_due_at(self, account: dict, now: datetime) -> datetime | None:
        if not str(account.get("refresh_token") or "").strip():
            return None
        if account.get("status") == "禁用":
            return None
        if self._recent_refresh_token_keepalive_error(account, now):
            return None
        anchor = self._refresh_token_keepalive_anchor(account)
        if anchor is None:
            return now
        due_at = anchor + timedelta(seconds=self._REFRESH_TOKEN_KEEPALIVE_SECONDS)
        return due_at if due_at <= now else None

    def _request_access_token_refresh(
            self,
            refresh_token: str,
            account: dict | None = None,
            timeout_secs: float | None = None,
    ) -> dict[str, str]:
        from curl_cffi import requests
        from services.proxy_service import proxy_settings

        session = requests.Session(**proxy_settings.build_session_kwargs(account=account, upstream=True, impersonate="chrome110", verify=True))
        try:
            if timeout_secs is not None and timeout_secs <= 0:
                raise TimeoutError("access token refresh timed out")
            request_timeout = (
                min(60.0, max(0.1, float(timeout_secs)))
                if timeout_secs is not None
                else 60.0
            )
            response = session.post(
                self._OAUTH_TOKEN_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": self._OAUTH_USER_AGENT,
                },
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._OAUTH_CLIENT_ID,
                    "redirect_uri": self._OAUTH_REDIRECT_URI,
                },
                timeout=request_timeout,
            )
            try:
                data = response.json()
            except ValueError:
                data = None
            access_token = data.get("access_token") if isinstance(data, dict) else None
            if response.status_code != 200 or not isinstance(access_token, str) or not access_token.strip():
                detail = ""
                if isinstance(data, dict):
                    error = data.get("error")
                    if isinstance(error, dict):
                        error = error.get("message") or error.get("code")
                    detail = str(data.get("error_description") or error or data.get("message") or "")
                detail = self._sanitize_token_refresh_error(detail, refresh_token, access_token) if detail else ""
                if not detail:
                    detail = "missing access_token" if isinstance(data, dict) else "non-JSON response"
                raise RuntimeError(f"oauth_refresh_http_{response.status_code}: {detail}")
            returned_rt = data.get("refresh_token")
            returned_id = data.get("id_token")
            tokens = {
                "access_token": access_token.strip(),
                "refresh_token": (returned_rt.strip() or refresh_token) if isinstance(returned_rt, str) else refresh_token,
                "id_token": returned_id.strip() if isinstance(returned_id, str) else "",
            }
            tokens.update({
                key: value for key, value in token_expiry_fields(
                    {**data, **tokens}, received_at=datetime.now(timezone.utc)
                ).items() if value is not None
            })
            return tokens
        finally:
            session.close()

    @staticmethod
    def _sanitize_token_refresh_error(error: object, *tokens: object) -> str:
        detail = str(error or "refresh token failed")
        for token in tokens:
            if token:
                detail = detail.replace(str(token), "[redacted]")
        detail = re.sub(r"\brt_[^\s\"'<>;,]+", "[redacted]", detail)
        detail = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted]", detail)
        return detail[:500]

    def _apply_refreshed_tokens(self, old_access_token: str, token_data: dict, event: str) -> str:
        now = datetime.now(timezone.utc).isoformat()
        with self._image_slot_condition:
            old_token = self._resolve_access_token_locked(old_access_token)
            current = self._accounts.get(old_token)
            if current is None:
                return old_token
            new_token = str(token_data.get("access_token") or old_token).strip()
            if not new_token:
                return old_token

            updates = {**token_data, "access_token": new_token}
            if token_data.get("refresh_token"):
                updates["refresh_token"] = str(token_data.get("refresh_token") or "").strip()
            else:
                updates.pop("refresh_token", None)
            if token_data.get("id_token"):
                updates["id_token"] = str(token_data.get("id_token") or "").strip()
            else:
                updates.pop("id_token", None)
            next_item = merge_account_credentials(current, updates, received_at=datetime.fromisoformat(now))
            next_item["last_token_refresh_at"] = now
            next_item["last_token_refresh_error"] = None
            next_item["last_token_refresh_error_at"] = None
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None

            account = self._normalize_account(next_item)
            if account is None:
                return old_token

            rotated = new_token != old_token
            if rotated:
                # A cache entry is tied to the exact access token. Do not let
                # callers with an old alias reuse a preflight proof after a
                # refresh has rotated that token.
                self._image_preflight_cache.pop(old_token, None)
                self._image_preflight_cache.pop(new_token, None)
                self._accounts.pop(old_token, None)
                self._token_aliases[old_token] = new_token
                old_inflight = int(self._image_inflight.pop(old_token, 0))
                if old_inflight:
                    self._image_inflight[new_token] = int(self._image_inflight.get(new_token, 0)) + old_inflight
            self._accounts[new_token] = account
            self._save_accounts()
            self._image_slot_condition.notify_all()

        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 已刷新 access_token",
            {"source": event, "token": anonymize_token(new_token), "rotated": rotated},
        )
        return new_token

    def refresh_access_token(
            self,
            access_token: str,
            *,
            force: bool = False,
            event: str = "refresh_access_token",
            timeout_secs: float | None = None,
            raise_on_error: bool = False,
    ) -> str:
        if not access_token:
            return ""
        deadline = (
            time.monotonic() + max(0.0, float(timeout_secs))
            if timeout_secs is not None
            else None
        )
        if deadline is None:
            acquired = self._token_refresh_lock.acquire()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._token_refresh_lock.acquire(timeout=remaining):
                raise TimeoutError("access token refresh timed out")
            acquired = True
        try:
            resolved_token, account = self._get_account_for_token(access_token)
            if not account:
                return access_token
            active_token = str(account.get("access_token") or resolved_token or access_token)
            if not self._token_needs_refresh(
                active_token, force=force, expires_at=account.get("access_token_expires_at")
            ):
                return active_token
            refresh_token = str(account.get("refresh_token") or "").strip()
            if not refresh_token:
                return active_token
            if not force and self._recent_token_refresh_error(account):
                recent_error = str(account.get("last_token_refresh_error") or "")
                if timeout_secs is not None and is_transient_image_preflight_transport_error(recent_error):
                    # A request-bound preflight must not reuse an expired token
                    # after a failed refresh and mistake its 401 for account
                    # invalidation. Preserve the existing refresh backoff.
                    raise ImagePreflightTransportError(recent_error)
                return active_token
            try:
                remaining = (deadline - time.monotonic()) if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("access token refresh timed out")
                token_data = self._request_access_token_refresh(
                    refresh_token,
                    account,
                    timeout_secs=remaining,
                )
            except Exception as exc:
                error_str = str(exc or "")
                if error_str:
                    error_str = self._sanitize_token_refresh_error(
                        error_str, active_token, refresh_token, account.get("id_token")
                    )
                if not error_str and is_transient_image_preflight_transport_error(exc):
                    error_str = (
                        "access token refresh timed out"
                        if _is_image_preflight_timeout(exc)
                        else "failed to connect while refreshing access token"
                    )
                self._record_token_refresh_error(active_token, event, error_str)
                if timeout_secs is not None and is_transient_image_preflight_transport_error(exc):
                    # Background maintenance keeps its best-effort behavior;
                    # image preflight instead reports the actual connection
                    # failure without probing the old credential afterwards.
                    raise ImagePreflightTransportError(exc) from exc
                if raise_on_error:
                    raise RuntimeError(error_str) from None
                # 如果是 app_session_terminated 错误，尝试密码重新登录
                if "app_session_terminated" in error_str.lower():
                    # 获取账号信息（email, password）
                    email = str(account.get("email") or "").strip()
                    password = str(account.get("password") or "").strip()
                    if email and password:
                        # 创建新线程执行密码重新登录
                        t = Thread(
                            target=self._password_re_login_thread,
                            args=(active_token, email, password, event),
                            daemon=True,
                        )
                        t.start()
                return active_token
            return self._apply_refreshed_tokens(active_token, token_data, event)
        finally:
            if acquired:
                self._token_refresh_lock.release()

    def refresh_account_tokens(self, access_tokens: list[str], progress_id: str | None = None) -> dict[str, Any]:
        tokens = list(dict.fromkeys(self.resolve_access_token(token) for token in access_tokens if token))
        if progress_id and self.get_refresh_progress(progress_id) is None:
            self.init_refresh_progress(progress_id, len(tokens))
        refreshed = 0
        skipped = 0
        errors = []
        for token in tokens:
            account = self.get_account(token)
            try:
                if account is None:
                    raise ValueError("account not found")
                if not str(account.get("refresh_token") or "").strip():
                    skipped += 1
                    continue
                self.refresh_access_token(token, force=True, event="manual_refresh_token", raise_on_error=True)
                if self.get_account(token) is None:
                    raise ValueError("account was removed during refresh")
                refreshed += 1
            except Exception as exc:
                errors.append({"token": anonymize_token(token), "error": self._sanitize_token_refresh_error(exc, token)})
            finally:
                if progress_id:
                    self.update_refresh_progress(progress_id, token)
        result = {"refreshed": refreshed, "skipped": skipped, "errors": errors, "items": self.list_accounts()}
        if progress_id:
            self.finish_refresh_progress(progress_id, result)
        return result

    def import_refresh_tokens(self, refresh_tokens: list[str], progress_id: str | None = None) -> dict[str, Any]:
        tokens = list(dict.fromkeys(token.strip() for token in refresh_tokens if token.strip()))
        if progress_id and self.get_refresh_progress(progress_id) is None:
            self.init_refresh_progress(progress_id, len(tokens))
        added = 0
        refreshed = 0
        errors = []
        warnings = []
        for index, refresh_token in enumerate(tokens, start=1):
            active_token = ""
            existing = None
            try:
                # Save a rotated RT before doing any optional account-info lookup.
                with self._token_refresh_lock:
                    with self._lock:
                        existing = next(
                            (dict(item) for item in self._accounts.values()
                             if str(item.get("refresh_token") or "").strip() == refresh_token),
                            None,
                        )
                    token_data = self._request_access_token_refresh(refresh_token, existing)
                    if existing is not None:
                        active_token = self._apply_refreshed_tokens(
                            existing["access_token"], token_data, "import_refresh_token"
                        )
                    else:
                        active_token = token_data["access_token"]
                        payload = {**token_data, "last_token_refresh_at": datetime.now(timezone.utc).isoformat()}
                        result = self.add_account_items([payload])
                        added += result["added"]
                    refreshed += 1
                try:
                    self.fetch_remote_info(
                        active_token, event="import_refresh_token", defer_invalid_removal=True, preserve_account=True
                    )
                except Exception as exc:
                    warnings.append({
                        "index": index,
                        "error": self._sanitize_token_refresh_error(exc, refresh_token, *token_data.values()),
                    })
            except Exception as exc:
                error = self._sanitize_token_refresh_error(exc, refresh_token)
                if existing is not None:
                    self._record_token_refresh_error(existing["access_token"], "import_refresh_token", error)
                errors.append({"index": index, "error": error})
            finally:
                if progress_id:
                    self.update_refresh_progress(progress_id, active_token)
        result = {
            "added": added,
            "refreshed": refreshed,
            "skipped": len(refresh_tokens) - len(tokens),
            "errors": errors,
            "warnings": warnings,
            "items": self.list_accounts(),
        }
        if progress_id:
            self.finish_refresh_progress(progress_id, result)
        return result

    def _password_re_login_thread(self, access_token: str, email: str, password: str, event: str, progress_id: str | None = None) -> None:
        """密码重新登录线程入口"""
        try:
            result = self._login_with_password(email, password)
            if result.get("ok"):
                # 登录成功，更新账号
                new_access_token = result.get("access_token", "")
                new_refresh_token = result.get("refresh_token", "")
                new_id_token = result.get("id_token", "")
                # 构建 token_data 供 _apply_refreshed_tokens 使用
                token_data = {
                    "access_token": new_access_token,
                    "refresh_token": new_refresh_token,
                    "id_token": new_id_token,
                }
                if isinstance(result.get("fp"), dict):
                    token_data["fp"] = result["fp"]
                for field in ("access_token_expires_at", "refresh_token_expires_at", "expires_at"):
                    if result.get(field) is not None:
                        token_data[field] = result[field]

                # 使用 _apply_refreshed_tokens 更新账号（处理 token 别名）
                new_token = self._apply_refreshed_tokens(access_token, token_data, f"{event}:password_relogin")

                # 额外更新 source_type 和 status（静默，避免重复日志）
                self.update_account(new_token, {
                    "source_type": result.get("source_type", "password"),
                    "status": "正常",
                }, quiet=True)

                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "更新账号",
                    {
                        "source": event,
                        "old_token": anonymize_token(access_token),
                        "new_token": anonymize_token(new_access_token),
                        "email": email,
                        "status": "成功",
                    },
                )
                if progress_id:
                    self.update_relogin_progress(progress_id, access_token, "成功")
            else:
                # 登录失败
                error_type = result.get("error", "")
                if error_type == "password_verify_failed_403" and isinstance(result.get("detail"), dict):
                    log_service.add(
                        LOG_TYPE_ACCOUNT,
                        "更新账号",
                        {
                            "source": event,
                            "token": anonymize_token(access_token),
                            "email": email,
                            "status": "失败",
                            "error": error_type,
                            "detail": result.get("detail", {}),
                        },
                    )
                    detail_error = result["detail"].get("error", {})
                    if isinstance(detail_error, dict) and detail_error.get("code") == "account_deactivated":
                        # 账号已删除/停用 → 标记为禁用
                        self.update_account(access_token, {"status": "禁用", "quota": 0}, quiet=True)
                        account = self.get_account(access_token) or {}
                        log_service.add(
                            LOG_TYPE_ACCOUNT,
                            "账号已停用-标记禁用",
                            {
                                "source": event,
                                "token": anonymize_token(access_token),
                                "email": email,
                                "detail": result.get("detail", {}),
                            },
                        )
                        if progress_id:
                            self.update_relogin_progress(progress_id, access_token, "禁用")
                    else:
                        # 永久故障：将账号标记为异常（或自动移除）
                        self.remove_invalid_token(access_token, f"{event}:password_relogin_failed", quiet=True)
                        if progress_id:
                            self.update_relogin_progress(progress_id, access_token, "异常", error_type)
                else:
                    log_service.add(
                        LOG_TYPE_ACCOUNT,
                        "更新账号",
                        {
                            "source": event,
                            "token": anonymize_token(access_token),
                            "email": email,
                            "status": "失败",
                            "error": error_type,
                            "detail": result.get("detail", {}),
                        },
                    )
                    # 永久故障：将账号标记为异常（或自动移除）
                    self.remove_invalid_token(access_token, f"{event}:password_relogin_failed", quiet=True)
                    if progress_id:
                        self.update_relogin_progress(progress_id, access_token, "异常", error_type)
        except Exception as exc:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "更新账号",
                {
                    "source": event,
                    "token": anonymize_token(access_token),
                    "email": email,
                    "status": "异常",
                    "error": str(exc),
                },
            )
            # 将账号标记为异常（或自动移除）
            self.remove_invalid_token(access_token, f"{event}:password_relogin_exception", quiet=True)
            if progress_id:
                self.update_relogin_progress(progress_id, access_token, "异常", str(exc))

    def _login_with_password(self, email: str, password: str) -> dict:
        """通过邮箱+密码登录，返回 {access_token, refresh_token, id_token, ...}"""
        from curl_cffi import requests
        
        # 常量
        auth_base = "https://auth.openai.com"
        platform_oauth_audience = "https://api.openai.com/v1"
        platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
        platform_oauth_client_id = self._OAUTH_CLIENT_ID
        platform_oauth_redirect_uri = "https://platform.openai.com/auth/callback"
        user_agent = self._OAUTH_USER_AGENT
        
        # 创建 session
        session_kwargs = {"impersonate": "chrome110", "verify": False}
        proxy = config.get_proxy_settings()
        if proxy:
            session_kwargs["proxy"] = proxy
        session = requests.Session(**session_kwargs)
        
        try:
            device_id = str(uuid.uuid4())
            
            # ─── 方式2: OAuth authorize 流程 ──────────────────────────
            # 使用 Platform Client + PKCE
            
            from utils.pkce import generate_pkce
            code_verifier, code_challenge = generate_pkce()
            
            # ② 发起 OAuth authorize 请求 (使用 Platform Client + PKCE)
            session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
            session.cookies.set("oai-did", device_id, domain="auth.openai.com")
            params = {
                "issuer": auth_base,
                "client_id": platform_oauth_client_id,
                "audience": platform_oauth_audience,
                "redirect_uri": platform_oauth_redirect_uri,
                "device_id": device_id,
                "screen_hint": "login_or_signup",
                "max_age": "0",
                "login_hint": email,
                "scope": "openid profile email offline_access",
                "response_type": "code",
                "response_mode": "query",
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "auth0Client": platform_auth0_client,
            }
            authorize_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
            resp = session.get(
                authorize_url,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "user-agent": user_agent,
                    "sec-ch-ua": '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-user": "?1",
                    "upgrade-insecure-requests": "1",
                    "referer": "https://platform.openai.com/",
                },
                allow_redirects=True,
                timeout=30,
            )
            
            if resp.status_code not in (200, 302):
                return {"ok": False, "error": f"authorize_failed_{resp.status_code}", "detail": {"url": resp.url, "text": resp.text[:500]}}
            
            # 检测最终 URL 是否指向错误页面
            final_url = str(resp.url)
            if "/error" in final_url and "payload=" in final_url:
                from urllib.parse import parse_qs, urlparse
                try:
                    parsed_query = parse_qs(urlparse(final_url).query)
                    error_payload_b64 = parsed_query.get("payload", [""])[0]
                    error_payload_b64 += "=" * ((4 - len(error_payload_b64) % 4) % 4)
                    error_payload = json.loads(base64.b64decode(error_payload_b64))
                    error_code = error_payload.get("errorCode", "")
                    if error_code == "rate_limit_exceeded":
                        return {"ok": False, "error": "rate_limit_exceeded", "detail": error_payload}
                    else:
                        return {"ok": False, "error": f"authorize_error_{error_code}", "detail": error_payload}
                except Exception as e:
                    return {"ok": False, "error": "authorize_redirect_error", "detail": {"url": final_url, "parse_error": str(e)}}
            
            # ③ 提交密码验证
            login_headers = {
                "accept": "application/json",
                "accept-language": "zh-CN,zh;q=0.9",
                "content-type": "application/json",
                "origin": auth_base,
                "priority": "u=1, i",
                "user-agent": user_agent,
                "sec-ch-ua": '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "referer": f"{auth_base}/email-verification",
                "oai-device-id": device_id,
            }
            
            # 添加 sentinel token
            try:
                from utils.sentinel import build_sentinel_token
                sentinel_val, oai_sc_val = build_sentinel_token(session, device_id, "password_verify")
                login_headers["openai-sentinel-token"] = sentinel_val
                if oai_sc_val:
                    session.cookies.set("oai-sc", oai_sc_val, domain=".openai.com")
            except Exception:
                pass
            
            login_resp = session.post(
                f"{auth_base}/api/accounts/password/verify",
                headers=login_headers,
                json={"password": password},
                timeout=30,
            )
            
            login_data = {}
            try:
                login_data = login_resp.json() if login_resp.text else {}
            except Exception:
                pass
            
            if login_resp.status_code != 200:
                error_code = login_data.get("error", {}).get("code", "")
                error_msg = login_data.get("error", {}).get("message", "")
                if error_code == "unsupported_country_region_territory":
                    return {"ok": False, "error": "unsupported_country_region_territory", "detail": login_data}
                elif error_code == "invalid_state":
                    return {"ok": False, "error": "invalid_state", "detail": login_data}
                elif "Invalid credentials" in error_msg or "wrong password" in error_msg.lower():
                    return {"ok": False, "error": "invalid_password", "detail": login_data}
                return {"ok": False, "error": f"password_verify_failed_{login_resp.status_code}", "detail": login_data}
            
            # 获取 authorization code
            continue_url = str(login_data.get("continue_url") or "").strip()
            auth_code = ""
            if continue_url:
                from urllib.parse import parse_qs, urlparse
                parsed_params = parse_qs(urlparse(continue_url).query)
                auth_code = str((parsed_params.get("code") or [""])[0]).strip()
            
            # ─── 处理邮箱 OTP 验证 ──────────────────────────
            if not auth_code:
                page_type = ""
                page_info = login_data.get("page")
                if isinstance(page_info, dict):
                    page_type = str(page_info.get("type") or "")
                
                if page_type == "email_otp_verification":
                    # 需要验证码才能登录，直接标记为账号异常
                    return {"ok": False, "error": "need_verification_code", "detail": login_data}
                else:
                    return {"ok": False, "error": "no_auth_code", "detail": login_data}
            
            # ④ 用 code 换 token (使用 Platform Client + code_verifier)
            platform_base = "https://platform.openai.com"
            token_resp = session.post(
                f"{auth_base}/api/accounts/oauth/token",
                headers={
                    "accept": "*/*",
                    "accept-language": "zh-CN,zh;q=0.9",
                    "auth0-client": platform_auth0_client,
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": platform_base,
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": f"{platform_base}/",
                    "sec-ch-ua": '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-site",
                    "user-agent": user_agent,
                },
                json={
                    "client_id": platform_oauth_client_id,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "redirect_uri": platform_oauth_redirect_uri,
                },
                verify=False,
                timeout=60,
            )
            
            token_data = {}
            try:
                token_data = token_resp.json() if token_resp.text else {}
            except Exception:
                pass
            
            if token_resp.status_code != 200 or not token_data.get("access_token"):
                return {"ok": False, "error": "token_exchange_failed", "detail": token_data}
            
            access_token = str(token_data.get("access_token") or "").strip()
            refresh_token = str(token_data.get("refresh_token") or "").strip()
            id_token = str(token_data.get("id_token") or "").strip()
            expiry_fields = token_expiry_fields(token_data, received_at=datetime.now(timezone.utc))
            
            # ⑤ 用 access_token 获取用户信息
            user_info = {}
            try:
                me_resp = session.get(
                    "https://chatgpt.com/backend-api/me",
                    headers={
                        "accept": "application/json",
                        "authorization": f"Bearer {access_token}",
                        "user-agent": user_agent,
                    },
                    timeout=30,
                )
                if me_resp.status_code == 200:
                    user_info = me_resp.json() if me_resp.text else {}
            except Exception:
                pass
            
            # 解析 JWT payload
            jwt_payload = self._decode_jwt_payload(access_token)
            
            email_from_jwt = str(jwt_payload.get("https://api.openai.com/profile", {}).get("email") or "").strip()
            account_id_from_jwt = str(
                jwt_payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or ""
            ).strip()
            
            account_info = user_info.get("account") if isinstance(user_info.get("account"), dict) else {}
            result = {
                "ok": True,
                "email": email_from_jwt or email,
                "account_id": account_id_from_jwt or account_info.get("account_id", ""),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expires_at": jwt_payload.get("exp"),
                "source_type": "password",
                "fp": {"oai-did": device_id, "oai-device-id": device_id},
            }
            result.update({key: value for key, value in expiry_fields.items() if value is not None})
            
            return result
        
        finally:
            session.close()

    def list_expiring_access_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for account in self._accounts.values()
                if str(account.get("refresh_token") or "").strip()
                and (token := str(account.get("access_token") or "").strip())
                and self._token_needs_refresh(token, expires_at=account.get("access_token_expires_at"))
            ]

    def list_refresh_token_keepalive_tokens(self) -> list[str]:
        now = datetime.now(timezone.utc)
        due_items: list[tuple[datetime, str]] = []
        with self._lock:
            for account in self._accounts.values():
                due_at = self._refresh_token_keepalive_due_at(account, now)
                token = str(account.get("access_token") or "").strip()
                if due_at is not None and token:
                    due_items.append((due_at, token))
        due_items.sort(key=lambda item: item[0])
        return [token for _, token in due_items[: self._REFRESH_TOKEN_KEEPALIVE_BATCH_SIZE]]

    def keepalive_refresh_tokens(self, access_tokens: list[str]) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        for access_token in access_tokens:
            before = self.resolve_access_token(access_token)
            after = self.refresh_access_token(before, force=True, event="refresh_token_keepalive")
            account = self.get_account(after)
            if account and str(account.get("last_token_refresh_error") or "").strip():
                errors.append({
                    "token": anonymize_token(before),
                    "error": str(account.get("last_token_refresh_error") or "refresh token failed"),
                })
                continue
            if account:
                refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
            "relogined": 0,
        }

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and self._account_matches_plan_type(item, plan_type)
               and self._account_matches_any_plan_type(item, plan_types)
               and self._account_matches_source_type(item, source_type)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        max_concurrency = 1 if config.image_success_cooldown_enabled else max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def _acquire_next_candidate_token(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            timeout_secs: float | None = None,
    ) -> str:
        self.restore_expired_image_cooldowns()
        deadline = time.monotonic() + max(0.0, float(timeout_secs)) if timeout_secs is not None else None
        with self._image_slot_condition:
            while True:
                remaining = (deadline - time.monotonic()) if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise RuntimeError("timed out waiting for an available image account")
                # Resolve exclusions again after waiting, because a failed
                # account can rotate its token before a slot becomes free.
                resolved_excluded = {
                    self._resolve_access_token_locked(token)
                    for token in (excluded_tokens or set())
                    if token
                }
                if not self._list_ready_candidate_tokens(resolved_excluded, plan_type, source_type, plan_types):
                    raise _ImageQuotaUnavailableError(
                        f"no available {plan_type or source_type or ''} image quota".replace("  ", " ").strip()
                        if plan_type or source_type else "no available image quota"
                    )
                tokens = self._list_available_candidate_tokens(resolved_excluded, plan_type, source_type, plan_types)
                if tokens:
                    access_token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                    return access_token
                self._image_slot_condition.wait(timeout=min(1.0, remaining) if remaining is not None else 1.0)

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()

    def get_available_access_token(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            excluded_tokens: set[str] | None = None,
            timeout_secs: float | None = None,
    ) -> str:
        """Reserve a verified image account, excluding failed account aliases."""
        from services.openai_backend_api import InvalidAccessTokenError

        deadline = time.monotonic() + max(0.0, float(timeout_secs)) if timeout_secs is not None else None
        max_attempts = 20
        externally_excluded = set(excluded_tokens or set())
        attempted_tokens: set[str] = set()
        last_invalid_error: InvalidAccessTokenError | None = None
        for _attempt in range(max_attempts):
            remaining = (deadline - time.monotonic()) if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise RuntimeError("timed out waiting for an available image account")
            try:
                access_token = self._acquire_next_candidate_token(
                    excluded_tokens=externally_excluded | attempted_tokens,
                    plan_type=plan_type,
                    source_type=source_type,
                    plan_types=plan_types,
                    timeout_secs=remaining,
                )
            except _ImageQuotaUnavailableError:
                # Exhausting candidates after authentication failures does not
                # establish that those accounts have exhausted their quota.
                if last_invalid_error is not None:
                    raise last_invalid_error from None
                raise
            attempted_tokens.add(access_token)
            reservation_owned = True
            try:
                preflight_timeout = (deadline - time.monotonic()) if deadline is not None else None
                if preflight_timeout is not None and preflight_timeout <= 0:
                    raise RuntimeError("timed out waiting for an available image account")
                account = self._preflight_image_account(
                    access_token,
                    event="get_available_access_token",
                    timeout_secs=preflight_timeout,
                )
                if deadline is not None and deadline - time.monotonic() <= 0:
                    raise RuntimeError("timed out waiting for an available image account")
                resolved = str((account or {}).get("access_token") or "")
                if resolved and resolved != access_token:
                    attempted_tokens.add(resolved)
                if (
                        self._is_image_account_available(account or {})
                        and self._account_matches_plan_type(account or {}, plan_type)
                        and self._account_matches_any_plan_type(account or {}, plan_types)
                        and self._account_matches_source_type(account or {}, source_type)
                ):
                    # Return with the reservation still held for the caller.
                    reservation_owned = False
                    return str((account or {}).get("access_token") or access_token)
            except InvalidAccessTokenError as exc:
                # Only confirmed invalid credentials justify trying another
                # account. Other failures (HTTP 403/429/5xx, transport, parsing,
                # etc.) must retain their cause instead of becoming quota errors.
                last_invalid_error = exc
            finally:
                # This intentionally covers BaseException / cancellation too.
                # If the reservation was not returned, it must be released.
                if reservation_owned:
                    self.release_image_slot(access_token)
        if last_invalid_error is not None:
            raise last_invalid_error
        raise _ImageQuotaUnavailableError(
            f"no available {plan_type or source_type or ''} image quota (tried {len(attempted_tokens)} tokens)".replace("  ", " ").strip()
            if plan_type or source_type else f"no available image quota (tried {len(attempted_tokens)} tokens)"
        )

    def get_text_access_token(
            self,
            excluded_tokens: set[str] | None = None,
            model: str = "auto",
    ) -> str:
        excluded = set(excluded_tokens or set())
        requested_model = str(model or "auto").strip() or "auto"
        route = None
        if requested_model != "auto":
            from services.model_service import model_catalog_service

            route = model_catalog_service.route_for_model(requested_model)
        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and (
                       route is None
                       or self._normalize_account_type(account.get("type")) in route.account_types
                   )
                   and (token := account.get("access_token") or "")
                   and token not in excluded
            ]
            if not candidates:
                if route is None or route.allow_anonymous:
                    return ""
                from services.model_service import ModelUnavailableError

                raise ModelUnavailableError(
                    f"model {requested_model!r} is not available to any active account"
                )
            access_token = candidates[self._index % len(candidates)]
            self._index += 1
        return self.refresh_access_token(access_token, event="get_text_access_token") or access_token

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    def remove_invalid_token(self, access_token: str, event: str, quiet: bool = False) -> bool:
        if not config.auto_remove_invalid_accounts:
            self.update_account(access_token, {"status": "异常", "quota": 0}, quiet=quiet)
            return False
        removed = bool(self.delete_accounts([access_token])["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0}, quiet=quiet)
        return removed

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        """返回所有账号的副本，并为每个账号附加当前图片在途数 image_inflight。

        image_inflight 为内存态并发计数(账号正在生成、尚未结束的图片数)。号池空闲时
        若某账号该值持续 > 0，说明其并发槽位泄漏、已被静默排除出调度，可借此在 UI 上诊断。
        """
        with self._lock:
            result = []
            for item in self._accounts.values():
                account = dict(item)
                token = account.get("access_token") or ""
                account["image_inflight"] = int(self._image_inflight.get(token, 0))
                result.append(account)
            return result

    def list_limited_tokens(self) -> list[str]:
        self.restore_expired_image_cooldowns()
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and not item.get("image_success_cooldown_until")
                   and not item.get("image_quota_cooldown_until")
                   and (token := item.get("access_token") or "")
            ]

    def restore_expired_image_cooldowns(self) -> int:
        """Restore accounts placed in temporary image cooldown after the deadline."""
        now = datetime.now(timezone.utc)
        restored = 0
        with self._image_slot_condition:
            for token, current in self._accounts.items():
                success_cooldown_until = self._parse_time(current.get("image_success_cooldown_until"))
                quota_cooldown_until = self._parse_time(current.get("image_quota_cooldown_until"))
                model_not_found_cooldown_until = self._parse_time(current.get("image_model_not_found_cooldown_until"))
                has_success_cooldown = bool(current.get("image_success_cooldown_until"))
                has_quota_cooldown = bool(current.get("image_quota_cooldown_until"))
                has_model_not_found_cooldown = bool(current.get("image_model_not_found_cooldown_until"))
                if not has_success_cooldown and not has_quota_cooldown and not has_model_not_found_cooldown:
                    continue
                success_still_active = (
                    has_success_cooldown
                    and config.image_success_cooldown_enabled
                    and (success_cooldown_until is None or success_cooldown_until > now)
                )
                quota_still_active = quota_cooldown_until is not None and quota_cooldown_until > now
                model_not_found_still_active = model_not_found_cooldown_until is not None and model_not_found_cooldown_until > now
                if success_still_active or quota_still_active or model_not_found_still_active:
                    continue
                next_item = dict(current)
                if has_success_cooldown:
                    next_item["image_success_cooldown_until"] = None
                if has_quota_cooldown:
                    next_item["image_quota_cooldown_until"] = None
                    next_item["image_quota_limited"] = False
                if has_model_not_found_cooldown:
                    next_item["image_model_not_found_cooldown_until"] = None
                if next_item.get("status") == "限流":
                    next_item["status"] = (
                        "正常" if int(next_item.get("quota") or 0) > 0
                        and not next_item.get("image_quota_limited") else "限流"
                    )
                self._invalidate_image_preflight_cache_locked(token)
                account = self._normalize_account(next_item)
                if account is not None:
                    self._accounts[token] = account
                    restored += 1
            if restored:
                self._save_accounts()
                self._image_slot_condition.notify_all()
        return restored

    def list_normal_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "正常"
                   and (token := item.get("access_token") or "")
            ]

    @staticmethod
    def _account_payload_token(item: dict) -> str:
        return str(item.get("access_token") or item.get("accessToken") or "").strip()

    @staticmethod
    def _prepare_account_payload(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = AccountService._account_payload_token(item)
        if not access_token:
            return None
        payload = dict(item)
        payload.pop("accessToken", None)
        payload["access_token"] = access_token
        # CPA/Codex 导出文件里的 `type=codex` 是导出格式，不是号池套餐类型。
        if str(payload.get("type") or "").strip().lower() == "codex":
            payload["export_type"] = "codex"
            payload["source_type"] = "codex"
            payload.pop("type", None)
        if str(payload.get("export_type") or "").strip().lower() == "codex":
            payload["source_type"] = "codex"
        if payload.get("plan_type") and not payload.get("type"):
            payload["type"] = str(payload.get("plan_type") or "").strip()
        return payload

    def add_account_items(self, items: list[dict]) -> dict:
        payloads = [
            payload
            for item in items
            if (payload := self._prepare_account_payload(item)) is not None
        ]
        return self._add_account_payloads(payloads)

    def add_accounts(self, tokens: list[str], source_type: str = "web") -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}
        return self._add_account_payloads([
            {"access_token": token, "source_type": self._normalize_source_type(source_type)}
            for token in tokens
        ])

    def _add_account_payloads(self, payloads: list[dict]) -> dict:
        deduped: dict[str, dict] = {}
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            access_token = self._account_payload_token(payload)
            if not access_token:
                continue
            current = deduped.get(access_token, {})
            deduped[access_token] = {**current, **payload, "access_token": access_token}

        if not deduped:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        with self._lock:
            added = 0
            skipped = 0
            for access_token, payload in deduped.items():
                current = self._accounts.get(access_token)
                if current is None:
                    added += 1
                    self._cumulative_total += 1
                    self._save_cumulative_total()
                    current = {"created_at": self._now()}
                else:
                    skipped += 1
                incoming = dict(payload)
                if not incoming.get("created_at"):
                    incoming.pop("created_at", None)
                incoming["access_token"] = access_token
                incoming["type"] = str(incoming.get("type") or current.get("type") or "free")
                account = self._normalize_account(merge_account_credentials(
                    current, incoming, received_at=datetime.now(timezone.utc)
                ))
                if account is not None:
                    self._invalidate_image_preflight_cache_locked(access_token)
                    self._accounts[access_token] = account
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()]
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped, "items": items}

    def delete_accounts(self, tokens: list[str]) -> dict:
        target_set = set(token for token in tokens if token)
        if not target_set:
            return {"removed": 0, "items": self.list_accounts()}
        with self._lock:
            target_set = {self._resolve_access_token_locked(token) for token in target_set if token}
            removed = sum(self._accounts.pop(token, None) is not None for token in target_set)
            for token in target_set:
                self._image_inflight.pop(token, None)
                self._invalidate_image_preflight_cache_locked(token)
            self._token_aliases = {
                old: new
                for old, new in self._token_aliases.items()
                if old not in target_set and new not in target_set
            }
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()]
        return {"removed": removed, "items": items}

    def update_account(
        self,
        access_token: str,
        updates: dict,
        quiet: bool = False,
        *,
        preserve_account: bool = False,
        preserve_quota: bool = False,
    ) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            account = self._normalize_account(merge_account_credentials(
                current, {**updates, "access_token": access_token}, received_at=datetime.now(timezone.utc)
            ))
            if account is None:
                return None
            self._invalidate_image_preflight_cache_locked(access_token)
            now = datetime.now(timezone.utc)
            quota_cooldown_until = self._parse_time(account.get("image_quota_cooldown_until"))
            quota_cooldown_active = quota_cooldown_until is not None and quota_cooldown_until > now
            quota_limited = bool(current.get("image_quota_limited"))
            if quota_cooldown_active:
                account["image_quota_limited"] = True
                if account.get("status") not in {"禁用", "异常"}:
                    account["status"] = "限流"
            if quota_limited and preserve_quota:
                upstream_still_limited = account.get("status") == "限流" or int(account.get("quota") or 0) <= 0
                if quota_cooldown_active or upstream_still_limited:
                    account["quota"] = int(current.get("quota") or 0)
                    account["image_quota_limited"] = True
                    if account.get("status") not in {"禁用", "异常"}:
                        account["status"] = "限流"
                else:
                    account["image_quota_limited"] = False
                    account["image_quota_cooldown_until"] = None
            elif quota_limited and not quota_cooldown_active and updates.get("status") == "正常":
                account["image_quota_limited"] = False
                account["image_quota_cooldown_until"] = None
            # Upstream refreshes must not release a local image cooldown.
            cooldown_until = self._parse_time(account.get("image_success_cooldown_until"))
            if (config.image_success_cooldown_enabled and cooldown_until is not None
                    and cooldown_until > now
                    and account.get("status") not in {"禁用", "异常"}):
                account["status"] = "限流"
            if (account.get("status") == "限流" and config.auto_remove_rate_limited_accounts
                    and not preserve_account
                    and not account.get("image_success_cooldown_until")
                    and not account.get("image_quota_limited")
                    and not quota_cooldown_active):
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            if not quiet:
                log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                                {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def _record_refresh_success(self, access_token: str) -> None:
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account

    def _should_defer_invalid_token(self, account: dict | None, now: datetime) -> bool:
        if not isinstance(account, dict):
            return False
        created_at = self._parse_time(account.get("created_at"))
        if created_at is not None and (now - created_at).total_seconds() < self._NEW_ACCOUNT_INVALID_GRACE_SECONDS:
            return True
        last_invalid_at = self._parse_time(account.get("last_invalid_at"))
        invalid_count = int(account.get("invalid_count") or 0)
        if invalid_count <= 1:
            return True
        if last_invalid_at is not None and (now - last_invalid_at).total_seconds() < self._INVALID_CONFIRM_SECONDS:
            return True
        return False

    def _record_invalid_token_seen(
        self,
        access_token: str,
        event: str,
        error: str,
        defer_invalid_removal: bool = True,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return True
            self._invalidate_image_preflight_cache_locked(access_token)
            should_defer = defer_invalid_removal and self._should_defer_invalid_token(current, now)
            next_item = dict(current)
            next_item["invalid_count"] = int(next_item.get("invalid_count") or 0) + 1
            next_item["last_invalid_at"] = now.isoformat()
            next_item["last_refresh_error"] = str(error or "invalid access token")
            next_item["last_refresh_error_at"] = now.isoformat()
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                self._save_accounts()
            if should_defer:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "暂缓标记异常账号",
                    {"source": event, "token": anonymize_token(access_token), "error": str(error or "")},
                )
                return False
        return True

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        if not access_token:
            return None
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            # Keep release and cooldown marking atomic to prevent a queued request
            # reserving the account between success and the status update.
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if success:
                next_item["success"] = int(next_item.get("success") or 0) + 1
                next_item["quota"] = max(0, int(next_item.get("quota") or 0) - 1)
                quota_cooldown_until = self._parse_time(next_item.get("image_quota_cooldown_until"))
                quota_cooldown_active = (
                    quota_cooldown_until is not None
                    and quota_cooldown_until > datetime.now(timezone.utc)
                )
                if config.image_success_cooldown_enabled:
                    if next_item.get("status") not in {"禁用", "异常"}:
                        next_item["status"] = "限流"
                    next_item["image_success_cooldown_until"] = (
                        datetime.now(timezone.utc) + timedelta(minutes=config.image_success_cooldown_minutes)
                    ).isoformat()
                elif next_item["quota"] == 0:
                    next_item["status"] = "限流"
                    next_item["restore_at"] = next_item.get("restore_at") or None
                elif (next_item.get("status") == "限流" and not quota_cooldown_active
                      and not next_item.get("image_quota_limited")):
                    next_item["status"] = "正常"
                    next_item["image_success_cooldown_until"] = None
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if (account.get("status") == "限流"
                    and config.auto_remove_rate_limited_accounts
                    and not account.get("image_success_cooldown_until")
                    and not account.get("image_quota_cooldown_until")
                    and not account.get("image_quota_limited")):
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)
        return None

    def mark_image_quota_exhausted(
        self,
        access_token: str,
        *,
        restore_at: str | None = None,
        error: str = "",
    ) -> dict | None:
        """Persist an upstream image-quota rejection for this account.

        ChatGPT can report an exhausted image quota inside a 200 SSE turn,
        while the local cached quota still looks positive.  Keep this state
        separate from a generic failed attempt so the scheduler will not pick
        the account again during the current quota window.
        """
        updates: dict[str, object] = {
            "status": "限流",
            "image_quota_cooldown_until": restore_at,
            "image_quota_limited": True,
        }
        if restore_at:
            updates["restore_at"] = restore_at
        if error:
            updates["last_image_error"] = str(error)[:1000]
            updates["last_image_error_at"] = datetime.now(timezone.utc).isoformat()
        return self.update_account(access_token, updates, quiet=True, preserve_account=True)

    def mark_image_model_not_found(self, access_token: str, *, error: str = "") -> dict | None:
        """Temporarily cool down an account that returned HTTP 404 Model not found
        during an actual auto image generation session.

        This is a local scheduling hint only: it does not clear real quota,
        does not mark the account permanently banned, and does not penalize
        proxy health. The account becomes eligible again after the cooldown.
        """
        cooldown_until = (
            datetime.now(timezone.utc)
            + timedelta(minutes=config.image_model_not_found_cooldown_minutes)
        ).isoformat()
        updates: dict[str, object] = {
            "image_model_not_found_cooldown_until": cooldown_until,
        }
        if error:
            updates["last_image_error"] = str(error)[:1000]
            updates["last_image_error_at"] = datetime.now(timezone.utc).isoformat()
        return self.update_account(access_token, updates, quiet=True, preserve_account=True)

    def fetch_remote_info(
        self,
        access_token: str,
        event: str = "fetch_remote_info",
        defer_invalid_removal: bool = True,
        timeout_secs: float | None = None,
        *,
        preserve_account: bool = False,
    ) -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        deadline = (
            time.monotonic() + max(0.0, float(timeout_secs))
            if timeout_secs is not None
            else None
        )

        def remaining() -> float | None:
            if deadline is None:
                return None
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("timed out validating image account")
            return value

        active_token = self.refresh_access_token(
            access_token,
            event=f"{event}:preflight",
            timeout_secs=remaining(),
        ) or access_token
        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
            backend = OpenAIBackendAPI(active_token)
            try:
                result = backend.get_user_info(timeout_secs=remaining())
            finally:
                backend.close()
        except InvalidAccessTokenError as exc:
            if preserve_account:
                raise
            refreshed_token = self.refresh_access_token(
                active_token,
                force=True,
                event=f"{event}:invalid_access_token",
                timeout_secs=remaining(),
            )
            if refreshed_token and refreshed_token != active_token:
                try:
                    backend = OpenAIBackendAPI(refreshed_token)
                    try:
                        result = backend.get_user_info(timeout_secs=remaining())
                    finally:
                        backend.close()
                except InvalidAccessTokenError as retry_exc:
                    if self._record_invalid_token_seen(
                        refreshed_token,
                        event,
                        str(retry_exc),
                        defer_invalid_removal=defer_invalid_removal,
                    ):
                        self.remove_invalid_token(refreshed_token, event)
                    raise
                active_token = refreshed_token
            else:
                if self._record_invalid_token_seen(
                    active_token,
                    event,
                    str(exc),
                    defer_invalid_removal=defer_invalid_removal,
                ):
                    self.remove_invalid_token(active_token, event)
                raise
        remaining()
        self._record_refresh_success(active_token)
        return self.update_account(
            active_token, result, preserve_account=preserve_account, preserve_quota=True,
        )

    # ---- 刷新进度追踪 ----

    def init_refresh_progress(self, progress_id: str, total: int) -> None:
        """初始化刷新进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "status_counts": {"正常": 0, "限流": 0, "异常": 0, "禁用": 0},
                "total_quota": 0,
            }

    def update_refresh_progress(self, progress_id: str, token: str) -> None:
        """刷新单个账号后，更新进度计数。"""
        account = self.get_account(token)
        status = str(account.get("status") or "正常").strip() if account else "正常"
        quota = max(0, int(account.get("quota") or 0)) if account else 0

        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["status_counts"][status] = progress["status_counts"].get(status, 0) + 1
            progress["total_quota"] += quota

    def finish_refresh_progress(self, progress_id: str, result: dict | None = None, error: str | None = None) -> None:
        """标记刷新完成。"""
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            if error:
                progress["error"] = error

    def get_refresh_progress(self, progress_id: str) -> dict | None:
        """查询刷新进度。"""
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            return dict(progress) if progress else None

    def clean_refresh_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress.pop(progress_id, None)

    # ---- 重新登录进度追踪 ----

    def init_relogin_progress(self, progress_id: str, total: int) -> None:
        """初始化重新登录进度记录。"""
        with self._relogin_progress_lock:
            self._relogin_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "results": [],
            }

    def update_relogin_progress(self, progress_id: str, token: str, status: str, error: str | None = None) -> None:
        """更新单个重新登录进度。当所有账号处理完毕时自动标记完成。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["results"].append({
                "token": anonymize_token(token),
                "status": status,
                "error": error,
            })
            if progress["processed"] >= progress["total"]:
                progress["done"] = True

    def finish_relogin_progress(self, progress_id: str, result: dict | None = None, error: str | None = None) -> None:
        """标记重新登录完成。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            if error:
                progress["error"] = error

    def get_relogin_progress(self, progress_id: str) -> dict | None:
        """查询重新登录进度。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            return dict(progress) if progress else None

    def clean_relogin_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._relogin_progress_lock:
            self._relogin_progress.pop(progress_id, None)

    def refresh_accounts(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        defer_invalid_removal: bool = True,
    ) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            items = self.list_accounts()
            result = {"refreshed": 0, "errors": [], "items": items, "relogined": 0}
            if progress_id:
                self.finish_refresh_progress(progress_id, result)
            return result

        refreshed = 0
        errors = []
        max_workers = min(10, len(access_tokens))

        if progress_id:
            self.init_refresh_progress(progress_id, len(access_tokens))

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {
                executor.submit(self.fetch_remote_info, token, "refresh_accounts", defer_invalid_removal): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                token = futures[future]
                try:
                    account = future.result()
                except (KeyboardInterrupt, SystemExit):
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    error_str = str(exc)
                    # TLS/代理连接错误是网络问题，不计入账号失败
                    from services.protocol.conversation import is_tls_connection_error
                    if not is_tls_connection_error(error_str):
                        errors.append({"token": anonymize_token(token), "error": error_str})
                else:
                    if account is not None:
                        refreshed += 1

                if progress_id:
                    self.update_refresh_progress(progress_id, token)
        except (KeyboardInterrupt, SystemExit):
            if progress_id:
                self.finish_refresh_progress(progress_id, error="cancelled")
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=True)

        # 自动重新登录异常账号（仅当配置开启时）
        relogined = 0
        if config.auto_relogin_after_refresh:
            for token in access_tokens:
                account = self.get_account(token)
                if not account:
                    continue
                status = str(account.get("status") or "").strip()
                if status != "异常":
                    continue
                email = str(account.get("email") or "").strip()
                password = str(account.get("password") or "").strip()
                if not email or not password:
                    continue
                t = Thread(
                    target=self._password_re_login_thread,
                    args=(token, email, password, "auto_relogin_after_refresh"),
                    daemon=True,
                )
                t.start()
                relogined += 1

        result = {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
            "relogined": relogined,
        }

        if progress_id:
            self.finish_refresh_progress(progress_id, result)

        return result

    def re_login_accounts(self, access_tokens: list[str], progress_id: str | None = None) -> dict[str, Any]:
        """对选中账号执行密码重新登录流程。

        仅对包含 email + password 的账号有效。
        登录成功后自动将状态设为"正常"。
        """
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            result = {"relogined": 0, "skipped": 0, "errors": [], "items": self.list_accounts()}
            if progress_id:
                self.finish_relogin_progress(progress_id, result)
            return result

        if progress_id:
            self.init_relogin_progress(progress_id, len(access_tokens))

        relogined = 0
        skipped = 0
        errors = []

        for token in access_tokens:
            account = self.get_account(token)
            if not account:
                errors.append({"token": anonymize_token(token), "error": "账号不存在"})
                if progress_id:
                    self.update_relogin_progress(progress_id, token, "跳过", "账号不存在")
                continue

            email = str(account.get("email") or "").strip()
            password = str(account.get("password") or "").strip()
            if not email or not password:
                skipped += 1
                if progress_id:
                    self.update_relogin_progress(progress_id, token, "跳过", "无邮箱密码")
                continue

            # 在新线程中执行密码重新登录
            t = Thread(
                target=self._password_re_login_thread,
                args=(token, email, password, "manual_relogin", progress_id),
                daemon=True,
            )
            t.start()
            relogined += 1

        result = {
            "relogined": relogined,
            "skipped": skipped,
            "errors": errors,
            "items": self.list_accounts(),
        }
        if progress_id:
            # 如果所有账号都已同步处理完毕（没有启动线程），直接标记完成
            if relogined == 0:
                self.finish_relogin_progress(progress_id, result)
            else:
                # 有线程在运行，等线程结束后再完成
                pass
        return result

    def build_export_items(self, access_tokens: list[str] | None = None) -> list[dict[str, str]]:
        target_tokens = set(token for token in (access_tokens or []) if token)
        with self._lock:
            accounts = [
                dict(item)
                for item in self._accounts.values()
                if not target_tokens or str(item.get("access_token") or "") in target_tokens
            ]

        items: list[dict[str, str]] = []
        for account in accounts:
            access_token = str(account.get("access_token") or "").strip()
            refresh_token = str(account.get("refresh_token") or "").strip()
            id_token = str(account.get("id_token") or "").strip()
            if not access_token or not refresh_token or not id_token:
                continue

            access_payload = self._decode_jwt_payload(access_token)
            id_payload = self._decode_jwt_payload(id_token)
            auth_claim = access_payload.get("https://api.openai.com/auth")
            auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
            profile_claim = access_payload.get("https://api.openai.com/profile")
            profile_claim = profile_claim if isinstance(profile_claim, dict) else {}

            email = (
                str(account.get("email") or "").strip()
                or str(profile_claim.get("email") or "").strip()
                or str(id_payload.get("email") or "").strip()
            )
            account_id = (
                str(account.get("account_id") or "").strip()
                or str(auth_claim.get("chatgpt_account_id") or "").strip()
                or str(account.get("user_id") or "").strip()
            )
            item = {
                "type": str(account.get("export_type") or "codex"),
                "email": email,
                "account_id": account_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expired": self._timestamp_to_iso(access_payload.get("exp")) or account.get("access_token_expires_at") or "",
                "last_refresh": self._timestamp_to_iso(access_payload.get("iat")),
            }
            for field in ("access_token_expires_at", "refresh_token_expires_at"):
                if account.get(field):
                    item[field] = account[field]
            password = str(account.get("password") or "").strip()
            if password:
                item["password"] = password
            items.append(item)
        return items

    def get_stats(self) -> dict:
        with self._lock:
            items = list(self._accounts.values())
        total = len(items)
        active = sum(1 for a in items if a.get("status") == "正常")
        limited = sum(1 for a in items if a.get("status") == "限流")
        abnormal = sum(1 for a in items if a.get("status") == "异常")
        disabled = sum(1 for a in items if a.get("status") == "禁用")
        total_quota = sum(max(0, int(a.get("quota") or 0)) for a in items if a.get("status") == "正常")
        total_success = sum(int(a.get("success") or 0) for a in items)
        total_fail = sum(int(a.get("fail") or 0) for a in items)
        by_type = {}
        for a in items:
            t = a.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": total,
            "cumulative_total": self._cumulative_total,
            "active": active,
            "limited": limited,
            "abnormal": abnormal,
            "disabled": disabled,
            "total_quota": total_quota,
            "total_success": total_success,
            "total_fail": total_fail,
            "by_type": by_type,
        }

    def account_health(self) -> dict:
        stats = self.get_stats()
        return {
            "healthy": stats["active"] > 0,
            "status": "ok" if stats["active"] > 0 else "degraded",
            **stats,
        }


account_service = AccountService(config.get_storage_backend())
