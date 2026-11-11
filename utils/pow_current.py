"""Sentinel handshake helpers (current 25-field format + FNV-1a PoW).

This replaces the legacy SHA3-based PoW in ``utils.pow`` for the live
chat-requirements flow. The proof token uses the current web format:

- 25-field config array
- ``d[3] = i`` iteration seed
- ``d[9] = elapsed_ms``
- ``fnv1a_32(seed + base64(json))`` hex prefix must be ``<= difficulty``
- returned token has the ``~S`` suffix

PoW solving is intentionally delegated to the local ``sentinel_solver``
service (real Chromium) whenever it is configured; the Python loop here is
only a last-resort fallback and can be slow for high difficulties.
"""
from __future__ import annotations

import base64
import json
import random
import time
from typing import Any, Sequence

from utils.pow import build_pow_config


def _fnv1a_32(text: str) -> str:
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 2246822507) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 3266489909) & 0xFFFFFFFF
    h ^= h >> 16
    return format(h & 0xFFFFFFFF, "08x")


def _b64(data: Any) -> str:
    return base64.b64encode(
        json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def _current_config(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
) -> list[Any]:
    """Build the current 25-field requirements config."""
    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build)
    if isinstance(config[0], (int, float)):
        config[0] = "1920x1080"
    return config


def build_requirements_token(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
) -> str:
    """Current requirements token: 25-field config, d[3]=1, random d[9], ``C`` prefix."""
    config = _current_config(user_agent, script_sources=script_sources, data_build=data_build)
    config[3] = 1
    config[9] = round(random.uniform(5, 50))
    return "gAAAAAC" + _b64(config)


def build_proof_token(
    seed: str,
    difficulty: str,
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    limit: int = 500_000,
) -> str:
    """Current PoW: FNV-1a 32-bit + avalanche, token ends with ``~S``.

    This Python loop is a fallback only; the primary path should call the
    sentinel solver service, which runs the same algorithm natively inside a
    real Chromium page.
    """
    config = _current_config(user_agent, script_sources=script_sources, data_build=data_build)
    difficulty = str(difficulty or "0")
    start = time.perf_counter()
    for i in range(limit):
        config[3] = i
        config[9] = round((time.perf_counter() - start) * 1000)
        payload = _b64(config)
        if _fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
            return "gAAAAAB" + payload + "~S"
    raise RuntimeError(f"failed to solve proof token: difficulty={difficulty}")
