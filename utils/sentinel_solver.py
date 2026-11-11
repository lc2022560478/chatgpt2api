"""Private Sentinel challenge solver client.

Calls a local `sentinel_solver` service (real Chromium) to solve PoW and
Turnstile challenges. The solver only receives challenge parameters
(seed/difficulty/dx/p) plus fingerprint hints; it never receives account
tokens, cookies, or user prompts.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from services.config import config
from utils.log import logger


class SentinelSolverError(RuntimeError):
    """Raised when the sentinel solver is unavailable or fails to solve."""


@dataclass
class SolverResult:
    proof_token: str = ""
    turnstile_token: str = ""


def _post(url: str, payload: Dict[str, Any], key: str, timeout: float) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/solve",
        data=body,
        headers={"Content-Type": "application/json", "X-Solver-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise SentinelSolverError(f"sentinel_solver http {exc.code}: {detail}") from exc
    except Exception as exc:
        raise SentinelSolverError(f"sentinel_solver unavailable: {exc}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        raise SentinelSolverError(f"sentinel_solver rejected: {str(data)[:500]}")
    return data


def solve(
    *,
    seed: str = "",
    difficulty: str = "",
    dx: str = "",
    p: str = "",
    user_agent: str = "",
    proxy: str = "",
    locale: str = "",
    timezone_id: str = "",
    screen_width: int = 0,
    screen_height: int = 0,
    timeout_secs: float = 60.0,
) -> SolverResult:
    """Ask the solver for proof and/or turnstile tokens."""
    if not config.sentinel_solver_enabled:
        raise SentinelSolverError("sentinel_solver is disabled")
    payload: Dict[str, Any] = {}
    if seed:
        payload["seed"] = seed
        payload["difficulty"] = difficulty or "0"
    if dx:
        payload["dx"] = dx
        payload["p"] = p
    if user_agent:
        payload["userAgent"] = user_agent
    if proxy:
        payload["proxy"] = proxy
    if locale:
        payload["locale"] = locale
    if timezone_id:
        payload["timezoneId"] = timezone_id
    if screen_width and screen_height:
        payload["screen"] = {"width": int(screen_width), "height": int(screen_height)}

    data = _post(
        config.sentinel_solver_url,
        payload,
        config.sentinel_solver_key,
        timeout_secs,
    )
    return SolverResult(
        proof_token=str(data.get("proofToken") or ""),
        turnstile_token=str(data.get("turnstileToken") or ""),
    )
