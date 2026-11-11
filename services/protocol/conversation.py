from __future__ import annotations

import base64
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Iterator

import tiktoken

from services.account_service import ImagePreflightTransportError, account_service
from services.config import config
from services.image_failure import extract_image_quota_restore_at, is_image_quota_error_text
from services.image_storage_service import ImageStorageError, image_storage_service
from services.openai_backend_api import (
    ImageContentPolicyError,
    ImagePollTimeoutError,
    ImageStreamHardTimeoutError,
    OpenAIBackendAPI,
)
from utils.helper import (
    IMAGE_MODELS,
    UpstreamHTTPError,
    extract_image_from_message_content,
    is_codex_image_model,
    is_supported_image_model,
    split_image_model,
)
from utils.image_tokens import count_image_content_tokens
from utils.log import logger


class ImageGenerationError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        error_type: str = "server_error",
        code: str | None = "upstream_error",
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
        raw_error: str = "",
        stage: str = "",
        attempt: int = 0,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.account_email = account_email
        self.conversation_id = conversation_id
        # Keep diagnostic context out of the public OpenAI-shaped error while
        # preserving it for retry decisions and structured server logs.
        self.raw_error = raw_error
        self.stage = stage
        self.attempt = attempt

    def to_openai_error(self) -> dict[str, Any]:
        error_dict = {
            "error": {
                "message": public_image_error_message(str(self)),
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }
        if self.account_email:
            error_dict["error"]["account_email"] = self.account_email
        return error_dict


def public_image_error_message(message: str) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    if "model not found" in lower:
        return "The upstream service rejected the conversation model (Model not found). Check the configured model and account access."
    if any(item in lower for item in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return "The image generation request failed. Please try again later."
    return text or "The image generation request failed. Please try again later."


def is_token_invalid_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "token_invalidated" in text
        or "token_revoked" in text
        or "authentication token has been invalidated" in text
        or "invalidated oauth token" in text
    )


def is_tls_connection_error(message: str) -> bool:
    """检测 TLS/SSL 连接错误，这类错误通常可以通过重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (7)" in text
        or "curl: (18)" in text
        or "curl: (35)" in text
        or "curl: (52)" in text
        or "curl: (55)" in text
        or "curl: (56)" in text
        or "curl: (92)" in text
        or "tls connect error" in text
        or "openssl_internal" in text
        or "ssl: wrong_version_number" in text
        or "ssl: certificate_verify_failed" in text
        or "connection aborted" in text
        or "connection closed abruptly" in text
        or "connection closed by peer" in text
        or "remote disconnected" in text
        or "connection reset by peer" in text
        or "recv failure" in text
        or "send failure" in text
        or "unexpected eof" in text
        or "broken pipe" in text
    )


def is_connection_timeout_error(message: str) -> bool:
    """检测连接超时错误（如 curl 28），这类错误可通过同账号短等待重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (28)" in text
        or "operation timed out" in text
        or "connection timed out" in text
        or "read timed out" in text
        or "connect timeout" in text
        or "preflight timed out" in text
        or "timed out validating image account" in text
        or "access token refresh timed out" in text
    )


def image_stream_error_message(message: str) -> str:
    text = str(message or "")
    if is_token_invalid_error(text):
        return "image generation failed"
    if is_tls_connection_error(text):
        return "upstream image connection failed, please retry later"
    if is_connection_timeout_error(text):
        return "upstream connection timed out, please retry later"
    return text or "image generation failed"


def _is_auto_image_model(model: str) -> bool:
    """Only the real auto image conversation path gets the 404 Model not found retry."""
    plan_type, base_model = split_image_model(model)
    normalized = str(base_model or model or "").strip().lower()
    return not plan_type and normalized in {"", "auto"}


def is_exact_image_model_not_found(exc: BaseException) -> bool:
    """True only for HTTP 404 whose JSON detail is exactly "Model not found"."""
    if not isinstance(exc, UpstreamHTTPError) or exc.status_code != 404:
        return False
    body = exc.body
    if isinstance(body, dict):
        return str(body.get("detail") or "") == "Model not found"
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            return False
        return isinstance(parsed, dict) and str(parsed.get("detail") or "") == "Model not found"
    return False


RETRYABLE_IMAGE_UPSTREAM_STATUS_CODES = {401, 403, 408, 423, 425, 429}
DETERMINISTIC_IMAGE_REQUEST_STATUS_CODES = {400, 404, 405, 413, 415, 422}
IMAGE_RESULT_POSTPROCESS_ERROR_CODE = "image_result_postprocess_failed"
# Account preflight is a liveness check, not image generation. It must leave
# enough of the end-to-end budget for the selected account to create an image.
IMAGE_ACCOUNT_PREFLIGHT_TIMEOUT_SECS = 30.0
# Transport retries intentionally do not throttle new plugin work. They only
# desynchronise retries after the upstream has already dropped a connection.
IMAGE_TRANSPORT_RETRY_BASE_SECS = 0.75
IMAGE_TRANSPORT_RETRY_MAX_SECS = 8.0
IMAGE_TRANSPORT_RETRY_MIN_REMAINING_SECS = 0.25


def _is_image_transport_error(error: Exception | str) -> bool:
    if isinstance(error, (UpstreamHTTPError, ImageContentPolicyError)):
        return False
    text = str(getattr(error, "raw_error", "") or error or "")
    lower = text.lower()
    return (
        isinstance(error, (ConnectionError, TimeoutError))
        or is_tls_connection_error(text)
        or is_connection_timeout_error(text)
        or "connection reset" in lower
        or "connection refused" in lower
        or "temporarily unavailable" in lower
    )


def _image_transport_retry_backoff_secs(attempt: int, request_deadline: float) -> float:
    """Return jittered retry delay while reserving a little request budget."""
    remaining = request_deadline - time.monotonic()
    if remaining <= IMAGE_TRANSPORT_RETRY_MIN_REMAINING_SECS:
        return 0.0
    exponent = min(max(0, attempt - 1), 4)
    base = min(IMAGE_TRANSPORT_RETRY_BASE_SECS * (2 ** exponent), IMAGE_TRANSPORT_RETRY_MAX_SECS)
    delay = base + random.uniform(0.0, base)
    return min(delay, max(0.0, remaining - IMAGE_TRANSPORT_RETRY_MIN_REMAINING_SECS))


def _is_retryable_image_failure(exc: Exception) -> bool:
    if isinstance(exc, (ImageContentPolicyError, ImageStorageError)):
        return False
    if isinstance(exc, UpstreamHTTPError):
        return (
            exc.status_code in RETRYABLE_IMAGE_UPSTREAM_STATUS_CODES
            or exc.status_code >= 500
            or is_exact_image_model_not_found(exc)
        )
    if isinstance(exc, ImagePollTimeoutError):
        return True
    if isinstance(exc, ImageGenerationError):
        if exc.code == "image_model_not_found":
            return True
        if exc.code in {"content_policy_violation", IMAGE_RESULT_POSTPROCESS_ERROR_CODE, "upstream_result_unavailable"}:
            return False
        if exc.status_code in DETERMINISTIC_IMAGE_REQUEST_STATUS_CODES:
            return False
        return exc.status_code >= 500 or exc.code in {
            "image_quota_exhausted",
            "upstream_error",
            "upstream_timeout",
            "no_image_generated",
        }

    text = str(exc or "")
    return is_token_invalid_error(text) or _is_image_transport_error(exc)


def _image_request_timeout_message() -> str:
    return f"图片生成请求总超时（已等待 {config.image_request_timeout_secs} 秒）。"


def _image_attempt_deadline(request: "ConversationRequest") -> float | None:
    deadlines = [
        deadline
        for deadline in (request.image_request_deadline, request.image_attempt_deadline)
        if deadline is not None
    ]
    return min(deadlines) if deadlines else None


def _image_stream_deadline(request: "ConversationRequest") -> float:
    configured_deadline = time.monotonic() + config.image_stream_timeout_secs
    attempt_deadline = _image_attempt_deadline(request)
    return min(configured_deadline, attempt_deadline) if attempt_deadline is not None else configured_deadline


def _image_stream_timeout_message(request: "ConversationRequest", deadline: float) -> str:
    if deadline == _image_attempt_deadline(request):
        return _image_timeout_message(request)
    return "图片生成上游流超时。"


def _image_timeout_message(request: "ConversationRequest") -> str:
    request_deadline = request.image_request_deadline
    attempt_deadline = request.image_attempt_deadline
    if (
            request_deadline is not None
            and attempt_deadline is not None
            and attempt_deadline < request_deadline
    ):
        return "图片生成当前账号超时。"
    return _image_request_timeout_message()


def _remaining_image_poll_budget(request: "ConversationRequest") -> float:
    """Return the remaining budget for the current account's image polling."""
    configured_timeout = float(config.image_poll_timeout_secs)
    deadline = _image_attempt_deadline(request)
    if deadline is None:
        return configured_timeout
    return min(configured_timeout, max(0.0, deadline - time.monotonic()))


def _remaining_image_attempt_budget(request: "ConversationRequest") -> float:
    deadline = _image_attempt_deadline(request)
    if deadline is None:
        return float("inf")
    return max(0.0, deadline - time.monotonic())


def _image_operation_timeout(request: "ConversationRequest", maximum: float) -> float:
    """Return a non-zero timeout for one upstream operation within the image budget."""
    remaining = _remaining_image_poll_budget(request)
    if remaining <= 0:
        raise ImagePollTimeoutError(_image_timeout_message(request))
    return min(float(maximum), max(0.1, remaining))


def _image_stream_operation_timeout(request: "ConversationRequest") -> float:
    remaining = _remaining_image_attempt_budget(request)
    if remaining <= 0:
        raise ImagePollTimeoutError(_image_timeout_message(request))
    return min(float(config.image_stream_timeout_secs), max(0.1, remaining))


def _monitor_image_stage(request: "ConversationRequest", stage: str, **details: object) -> None:
    """Publish optional image lifecycle state without affecting generation."""
    call_id = str(request.monitor_call_id or "").strip()
    if not call_id:
        return
    try:
        from services.realtime_monitor_service import realtime_monitor_service

        realtime_monitor_service.stage(call_id, stage, **details)
    except Exception:
        # Observability must never change the result of an image request.
        return


REFERENCED_IMAGE_IDS_RE = re.compile(r'"referenced_image_ids"\s*:\s*\[([^\]]+)\]')
# 检测模型返回的部分工具调用 JSON（如 {"size":"1920x1088","n":1}）
# 这些 JSON 包含图片生成工具的参数，但没有实际生成图片
TOOL_PARAMS_JSON_RE = re.compile(
    r'\{\s*"size"\s*:\s*"\d+x\d+"\s*,\s*"n"\s*:\s*\d+\s*\}'
)


def is_model_text_reply_instead_of_image(message: str) -> bool:
    """检测模型是否返回了文本回复（包含工具调用 JSON）而非实际生成图片。

    当上游 ChatGPT 未能触发图片生成工具时，会返回一段描述性文本，
    其中可能包含 JSON 参数（如 prompt、referenced_image_ids、size/n 等）。
    这种情况应被视为「上游未生成图片」而非「内容策略违规」。

    检测两种模式：
    1. 完整的工具调用 JSON（含 referenced_image_ids）
    2. 部分的工具参数 JSON（如 {"size":"1920x1088","n":1}）
    """
    if not message:
        return False
    if REFERENCED_IMAGE_IDS_RE.search(message):
        return True
    # 检测部分工具参数 JSON（模型返回了工具参数但未触发工具）
    if TOOL_PARAMS_JSON_RE.search(message):
        return True
    return False


def encode_images(images: Iterable[tuple[bytes, str, str]]) -> list[str]:
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]


def save_image_bytes(image_data: bytes, base_url: str | None = None) -> str:
    return image_storage_service.save(image_data, base_url).url


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and str(item.get("type") or "") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def normalize_messages(messages: object, system: Any = None) -> list[dict[str, Any]]:
    normalized = []
    if config.global_system_prompt:
        normalized.append({"role": "system", "content": config.global_system_prompt})
    system_text = message_text(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            text = message_text(content)
            images: list[tuple[bytes, str]] = []
            if role == "user":
                images.extend(extract_image_from_message_content(content))
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "image":
                            continue
                        data = part.get("data")
                        if isinstance(data, (bytes, bytearray)) and all(existing[0] != bytes(data) for existing in images):
                            images.append((bytes(data), str(part.get("mime") or "image/png")))
            if images:
                parts: list[Any] = []
                if text:
                    parts.append({"type": "text", "text": text})
                for data, mime in images:
                    parts.append({"type": "image", "data": data, "mime": mime})
                normalized.append({"role": role, "content": parts})
            else:
                normalized.append({"role": role, "content": text})
    return normalized


def prompt_with_global_system(prompt: str) -> str:
    return f"{config.global_system_prompt}\n\n{prompt}" if config.global_system_prompt else prompt


def assistant_history_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")


def assistant_history_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in messages if item.get("role") == "assistant" and item.get("content")]


def build_image_prompt(prompt: str, size: str | None, quality: str = "auto") -> str:
    hints = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    if quality:
        hints.append(f"输出图片质量为 {quality}。")
    return f"{prompt.strip()}\n\n{''.join(hints)}" if hints else prompt


def encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


def count_message_image_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return sum(count_image_content_tokens(message.get("content"), model) for message in messages)


def count_message_text_tokens(messages: list[dict[str, Any]], model: str) -> int:
    encoding = encoding_for_model(model)
    total = 0
    for message in messages:
        total += 3
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                total += len(encoding.encode(message_text(value)))
            elif isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                continue
            if key == "name":
                total += 1
    return total + 3


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return count_message_text_tokens(messages, model) + count_message_image_tokens(messages, model)


def count_text_tokens(text: str, model: str) -> int:
    return len(encoding_for_model(model).encode(text))


def format_image_result(
    items: list[dict[str, Any]],
    prompt: str,
    response_format: str,
    base_url: str | None = None,
    created: int | None = None,
    message: str = "",
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for item in items:
        b64_json = str(item.get("b64_json") or "").strip()
        if not b64_json:
            continue
        revised_prompt = str(item.get("revised_prompt") or prompt).strip() or prompt
        if response_format == "b64_json":
            data.append({
                "b64_json": b64_json,
                "url": save_image_bytes(base64.b64decode(b64_json), base_url),
                "revised_prompt": revised_prompt,
            })
        else:
            data.append({
                "url": save_image_bytes(base64.b64decode(b64_json), base_url),
                "revised_prompt": revised_prompt,
            })
    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if message and not data:
        result["message"] = message
    return result


def _download_and_format_generated_images(
        backend: OpenAIBackendAPI,
        image_urls: list[str],
        request: ConversationRequest,
) -> list[dict[str, Any]]:
    """Do not regenerate an image after upstream result delivery fails locally."""
    try:
        image_items = [
            {"b64_json": base64.b64encode(image_data).decode("ascii")}
            for image_data in backend.download_image_bytes(image_urls)
        ]
        return format_image_result(
            image_items,
            request.prompt,
            request.response_format,
            request.base_url,
            int(time.time()),
        )["data"]
    except Exception as exc:
        raise ImageGenerationError(
            "image was generated but its result could not be retrieved",
            code=IMAGE_RESULT_POSTPROCESS_ERROR_CODE,
        ) from exc


@dataclass
class ConversationRequest:
    model: str = "auto"
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    thinking_effort: str = ""
    images: list[str] | None = None
    n: int = 1
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    base_url: str | None = None
    message_as_error: bool = False
    progress_callback: Any = None  # Callable[[str], None] | None
    monitor_call_id: str = ""
    # Set once by stream_image_outputs_with_pool so serial and parallel image
    # generation share one end-to-end budget.
    image_request_deadline: float | None = None
    # Set per account attempt to reserve time for a possible account failover.
    image_attempt_deadline: float | None = None


@dataclass
class ConversationState:
    text: str = ""
    raw_text: str = ""
    conversation_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    tool_invoked: bool | None = None
    turn_use_case: str = ""


@dataclass
class ImageOutput:
    kind: str
    model: str
    index: int
    total: int
    created: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    upstream_event_type: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    conversation_id: str = ""

    def to_chunk(self) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "image.generation.chunk",
            "created": self.created,
            "model": self.model,
            "index": self.index,
            "total": self.total,
            "progress_text": self.text,
            "upstream_event_type": self.upstream_event_type,
            "data": [],
        }
        if self.account_email:
            chunk["_account_email"] = self.account_email
        if self.conversation_id:
            chunk["_conversation_id"] = self.conversation_id
        if self.kind == "message":
            chunk.update({
                "object": "image.generation.message",
                "message": self.text,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        elif self.kind == "result":
            chunk.update({
                "object": "image.generation.result",
                "data": self.data,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        return chunk


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if isinstance(parts, list) and parts:
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    # Fallback: content_type "code" stores text in the "text" field instead of "parts"
    text_field = str(content.get("text") or "")
    if text_field:
        return text_field
    return ""


def is_visible_assistant_message(message: dict[str, Any]) -> bool:
    """Return whether an upstream assistant message is intended for the user."""
    author = message.get("author")
    if not isinstance(author, dict):
        return False
    role = str(author.get("role") or "").strip().lower()
    if role != "assistant":
        return False

    metadata = message.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("is_visually_hidden_from_conversation") is True:
        return False

    # Tool commands are emitted as assistant messages addressed to recipients
    # such as "web". Only messages addressed to everyone are user-visible.
    recipient = str(message.get("recipient") or "").strip().lower()
    if recipient and recipient != "all":
        return False

    # Reasoning and other internal channels must not leak into API text output.
    channel = str(message.get("channel") or "").strip().lower()
    if channel and channel != "final":
        return False

    return True


def strip_history(text: str, history_text: str = "") -> str:
    text = str(text or "")
    history_text = str(history_text or "")
    while history_text and text.startswith(history_text):
        text = text[len(history_text):]
    return text


def sanitize_output_text(text: str) -> str:
    text = str(text or "")

    def is_internal_annotation_part(part: str) -> bool:
        value = part.strip()
        if not value:
            return True
        lower = value.lower()
        return bool(
            re.fullmatch(r"turn\d+[a-z]*\d*", lower)
            or re.fullmatch(r"turn\d+\w*", lower)
            or lower.startswith(("turn", "source", "sources"))
        )

    def readable_annotation_part(parts: list[str]) -> str:
        for part in parts:
            value = part.strip()
            if value and not is_internal_annotation_part(value):
                return value
        return ""

    def annotation_text(payload: str) -> str:
        parts = [part.strip() for part in payload.split("\ue202")]
        kind = (parts[0] if parts else "").lower()
        data = parts[1:]
        if kind == "url":
            label = data[0] if data else ""
            url = data[1] if len(data) > 1 else ""
            if label and url.startswith(("http://", "https://")):
                return f"{label} ({url})"
            return label or url
        if kind == "cite":
            return readable_annotation_part(data)
        return readable_annotation_part(data)

    def replace_annotation(match: re.Match[str]) -> str:
        return annotation_text(match.group(1))

    def replace_annotation_before_punctuation(match: re.Match[str]) -> str:
        leading_space = match.group(1)
        replacement = annotation_text(match.group(2))
        return f"{leading_space}{replacement}" if replacement else ""

    # ChatGPT web sometimes returns rich annotation markers using private-use
    # characters. API clients cannot render those. Preserve readable labels
    # from entity/link annotations, while removing internal citation pointers.
    text = re.sub(r"(\s*)\ue200([^\ue201]*)\ue201(?=[.,;:!?])", replace_annotation_before_punctuation, text)
    text = re.sub(r"\ue200([^\ue201]*)\ue201", replace_annotation, text)
    text = re.sub(r"\ue200[^\ue201]*$", "", text)
    return text


def assistant_raw_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if not isinstance(message, dict) or not is_visible_assistant_message(message):
            continue
        text = assistant_message_text(message)
        if text:
            return strip_history(text, history_text)
    return apply_text_patch(event, current_text, history_text)


def assistant_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    return sanitize_output_text(assistant_raw_text(event, current_text, history_text))


def event_assistant_text(event: dict[str, Any], history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict) and is_visible_assistant_message(message):
            return strip_history(assistant_message_text(message), history_text)
    return ""


def apply_text_patch(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    if event.get("p") == "/message/content/parts/0":
        return apply_patch_op(event, current_text, history_text)

    operations = event.get("v")
    if isinstance(operations, str) and current_text and not event.get("p") and not event.get("o"):
        return current_text + operations

    if event.get("o") == "patch" and isinstance(operations, list):
        text = current_text
        for item in operations:
            if isinstance(item, dict):
                text = apply_text_patch(item, text, history_text)
        return text

    if not isinstance(operations, list):
        return current_text

    text = current_text
    for item in operations:
        if isinstance(item, dict):
            text = apply_text_patch(item, text, history_text)
    return text


def apply_patch_op(operation: dict[str, Any], current_text: str, history_text: str = "") -> str:
    op = operation.get("o")
    value = str(operation.get("v") or "")
    if op == "append":
        return current_text + value
    if op == "replace":
        return strip_history(value, history_text)
    return current_text


def add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
# 用于过滤非图片文件 ID（如 file_upload_business_upsell）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    # Negative lookahead excludes "file-service" (URI prefix, not a real id).
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    # 只提取真正的图片文件 ID（file_00000000... 格式），过滤非图片文件 ID（如 file_upload_business_upsell）
    add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids


def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    content = message.get("content") or {}
    if author.get("role") != "tool":
        return False
    if metadata.get("async_task_type") == "image_gen":
        return True
    if content.get("content_type") != "multimodal_text":
        return False
    return any(
        isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
        )
        for part in content.get("parts") or []
    )


def _is_user_message_event(event: dict[str, Any]) -> bool:
    """检查事件是否来自 user 角色消息。"""
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if isinstance(message, dict):
        author = message.get("author") or {}
        if str(author.get("role") or "").strip().lower() == "user":
            return True
    return False


def update_conversation_state(state: ConversationState, payload: str, event: dict[str, Any] | None = None) -> None:
    conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)
    if conversation_id and not state.conversation_id:
        state.conversation_id = conversation_id
    # Accept file_id / sediment_id when any of:
    #   1) event is a complete image_gen tool message
    #   2) prior server_ste_metadata already flipped tool_invoked True (in an image_gen turn),
    #      BUT only for non-user messages — user messages contain the uploaded input image
    #      which must NOT be treated as a generated output.
    #   3) patch event whose payload references asset_pointer / file-service://,
    #      BUT only when the event is not a user message.
    is_patch_event = isinstance(event, dict) and event.get("o") == "patch"
    is_user_msg = isinstance(event, dict) and _is_user_message_event(event)
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (state.tool_invoked is True and not is_user_msg)
        or (is_patch_event and not is_user_msg and ("asset_pointer" in payload or "file-service://" in payload))
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)
    if not isinstance(event, dict):
        return
    state.conversation_id = str(event.get("conversation_id") or state.conversation_id)
    value = event.get("v")
    if isinstance(value, dict):
        state.conversation_id = str(value.get("conversation_id") or state.conversation_id)
    if event.get("type") == "moderation":
        moderation = event.get("moderation_response")
        if isinstance(moderation, dict) and moderation.get("blocked") is True:
            state.blocked = True
    if event.get("type") == "server_ste_metadata":
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            if isinstance(metadata.get("tool_invoked"), bool):
                state.tool_invoked = metadata["tool_invoked"]
            state.turn_use_case = str(metadata.get("turn_use_case") or state.turn_use_case)


def conversation_base_event(event_type: str, state: ConversationState, **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "text": state.text,
        "conversation_id": state.conversation_id,
        "file_ids": list(state.file_ids),
        "sediment_ids": list(state.sediment_ids),
        "blocked": state.blocked,
        "tool_invoked": state.tool_invoked,
        "turn_use_case": state.turn_use_case,
        **extra,
    }


def iter_conversation_payloads(
        payloads: Iterator[str],
        history_text: str = "",
        history_messages: list[str] | None = None,
        deadline: float | None = None,
        timeout_message: str = "",
) -> Iterator[dict[str, Any]]:
    state = ConversationState()
    history_messages = history_messages or []
    history_index = 0
    for payload in payloads:
        if deadline is not None and deadline <= time.monotonic():
            raise ImagePollTimeoutError(timeout_message or _image_request_timeout_message())
        # print(f"[upstream_sse] {payload}", flush=True)
        if not payload:
            continue
        if payload == "[DONE]":
            yield conversation_base_event("conversation.done", state, done=True)
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            update_conversation_state(state, payload)
            yield conversation_base_event("conversation.raw", state, payload=payload)
            continue
        if not isinstance(event, dict):
            yield conversation_base_event("conversation.event", state, raw=event)
            continue
        update_conversation_state(state, payload, event)
        if history_index < len(history_messages) and event_assistant_text(event, history_text) == history_messages[history_index]:
            history_index += 1
            state.raw_text = ""
            state.text = ""
            continue
        next_raw_text = assistant_raw_text(event, state.raw_text, history_text)
        next_text = sanitize_output_text(next_raw_text)
        state.raw_text = next_raw_text
        if next_text != state.text:
            delta = next_text[len(state.text):] if next_text.startswith(state.text) else next_text
            state.text = next_text
            yield conversation_base_event("conversation.delta", state, raw=event, delta=delta)
            continue
        yield conversation_base_event("conversation.event", state, raw=event)


def conversation_events(
    backend: OpenAIBackendAPI,
    messages: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prompt: str = "",
    images: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
    thinking_effort: str = "",
    deadline: float | None = None,
    timeout_message: str = "",
) -> Iterator[dict[str, Any]]:
    normalized = normalize_messages(messages or ([{"role": "user", "content": prompt}] if prompt else []))
    image_model = is_supported_image_model(model)
    history_text = "" if image_model else assistant_history_text(normalized)
    history_messages = [] if image_model else assistant_history_messages(normalized)
    final_prompt = prompt_with_global_system(build_image_prompt(prompt, size, quality)) if image_model else prompt
    timeout_secs = None
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ImagePollTimeoutError(timeout_message or _image_request_timeout_message())
        timeout_secs = max(0.1, remaining)
    payloads = backend.stream_conversation(
        messages=normalized,
        model=model,
        prompt=final_prompt,
        images=images if image_model else None,
        system_hints=["picture_v2"] if image_model else None,
        thinking_effort=thinking_effort if not image_model else "",
        timeout_secs=timeout_secs,
    )
    yield from iter_conversation_payloads(
        payloads,
        history_text,
        history_messages,
        deadline=deadline,
        timeout_message=timeout_message,
    )


def text_backend(model: str = "auto") -> OpenAIBackendAPI:
    return OpenAIBackendAPI(access_token=account_service.get_text_access_token(model=model))


def stream_text_deltas(backend: OpenAIBackendAPI, request: ConversationRequest) -> Iterator[str]:
    attempted_tokens: set[str] = set()
    token = getattr(backend, "access_token", "")
    emitted = False
    while True:
        if token and token in attempted_tokens:
            raise RuntimeError("no available text account")
        if token:
            attempted_tokens.add(token)
        active_backend = None
        try:
            active_backend = OpenAIBackendAPI(access_token=token)
            for event in conversation_events(
                active_backend,
                messages=request.messages,
                model=request.model,
                prompt=request.prompt,
                thinking_effort=request.thinking_effort,
            ):
                if event.get("type") != "conversation.delta":
                    continue
                delta = str(event.get("delta") or "")
                if delta:
                    emitted = True
                    yield delta
            account_service.mark_text_used(token)
            return
        except Exception as exc:
            error_message = str(exc)
            if token and not emitted and is_token_invalid_error(error_message):
                refreshed_token = account_service.refresh_access_token(token, force=True, event="text_stream")
                if refreshed_token and refreshed_token != token and refreshed_token not in attempted_tokens:
                    token = refreshed_token
                else:
                    account_service.remove_invalid_token(token, "text_stream")
                    token = account_service.get_text_access_token(
                        excluded_tokens=set(attempted_tokens),
                        model=request.model,
                    )
                if token:
                    continue
            raise
        finally:
            if active_backend is not None:
                active_backend.close()


def collect_text(backend: OpenAIBackendAPI, request: ConversationRequest) -> str:
    return "".join(stream_text_deltas(backend, request))


def _get_detailed_error_from_tasks(
    backend: OpenAIBackendAPI,
    conversation_id: str,
    timeout_secs: float = 10.0,
    wait_secs: float = 2.0,
) -> str:
    """从 /backend-api/tasks/ 接口获取结构化错误信息。

    当 SSE 流检测到 moderation 拦截时，轮询 tasks 接口获取详细错误文本。
    使用结构化字段（metadata.is_error, author.role, content.content_type）判断，
    而非依赖易变的文本匹配。

    参数：
    - `backend`：OpenAIBackendAPI 实例。
    - `conversation_id`：会话 ID。
    - `timeout_secs`：请求超时秒数。
    - `wait_secs`：等待任务创建的秒数。设为 0 可跳过等待。

    返回：
    - 详细错误信息文本，如果未找到则返回空字符串。
    """
    import time as _time
    try:
        if wait_secs > 0:
            _time.sleep(wait_secs)
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=timeout_secs)
        if not tasks:
            return ""

        for task in tasks:
            is_error, error_msg, metadata = backend.check_task_error(task)
            if is_error and error_msg:
                logger.info({
                    "event": "image_task_structured_error",
                    "conversation_id": conversation_id,
                    "error_msg": error_msg,
                    "metadata": metadata,
                })
                return error_msg
        return ""
    except Exception as exc:
        logger.warning({
            "event": "image_task_error_query_failed",
            "conversation_id": conversation_id,
            "error": str(exc),
        })
        return ""


def _remove_image_conversation_later(
        backend: OpenAIBackendAPI,
        conversation_id: str,
        *,
        success: bool,
) -> None:
    if not conversation_id:
        return
    if not (config.image_remove_conversation_always or (success and config.image_remove_conversation_after_result)):
        return

    def _run() -> None:
        try:
            backend.delete_conversation(conversation_id)
            logger.info({"event": "image_conversation_removed", "conversation_id": conversation_id})
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_remove_failed",
                "conversation_id": conversation_id,
                "error": str(exc),
            })

    threading.Thread(target=_run, name=f"remove-image-conversation-{conversation_id}", daemon=True).start()


def stream_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    last: dict[str, Any] = {}
    stream_interrupted = False
    stream_deadline = _image_stream_deadline(request)
    timeout_message = _image_stream_timeout_message(request, stream_deadline)
    events = conversation_events(
            backend,
            prompt=request.prompt,
            model=request.model,
            images=request.images or [],
            size=request.size,
            quality=request.quality,
            deadline=stream_deadline,
            timeout_message=timeout_message,
    )
    try:
        for event in events:
            last = event
            if event.get("type") == "conversation.delta":
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    text=str(event.get("delta") or ""),
                    upstream_event_type="conversation.delta",
                    conversation_id=str(event.get("conversation_id") or ""),
                )
                continue
            if event.get("type") == "conversation.event":
                raw = event.get("raw")
                raw_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    upstream_event_type=raw_type,
                    conversation_id=str(event.get("conversation_id") or ""),
                )
    except Exception as exc:
        conversation_id = str(last.get("conversation_id") or "")
        if conversation_id and not getattr(exc, "conversation_id", ""):
            try:
                exc.conversation_id = conversation_id
            except (AttributeError, TypeError):
                pass
        # Losing the SSE connection does not cancel the upstream image job.
        # Continue that same conversation within the existing time budget;
        # never resubmit it merely because the progress stream disconnected.
        if (
                not conversation_id
                or last.get("blocked")
                or not (_is_image_transport_error(exc) or isinstance(exc, ImageStreamHardTimeoutError))
                or _remaining_image_poll_budget(request) <= 0
        ):
            raise
        stream_interrupted = True
        logger.warning({
            "event": "image_stream_interrupted_recovering",
            "conversation_id": conversation_id,
            "error_type": type(exc).__name__,
            "raw_error": str(getattr(exc, "raw_error", "") or exc)[:1000],
            "remaining_secs": _remaining_image_poll_budget(request),
            "has_result_ids": bool(last.get("file_ids") or last.get("sediment_ids")),
        })
        _monitor_image_stage(
            request, "resolving_image", conversation_id=conversation_id,
            recovery_from="stream_interruption",
        )
    finally:
        close_events = getattr(events, "close", None)
        if close_events is not None:
            close_events()

    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()
    logger.info({
        "event": "image_stream_resolve_start",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "tool_invoked": last.get("tool_invoked"),
        "turn_use_case": last.get("turn_use_case"),
    })
    if request.progress_callback:
        request.progress_callback("image_stream_resolve_start")
    if message and not file_ids and not sediment_ids and last.get("blocked"):
        # 尝试从 /backend-api/tasks/ 获取详细错误信息
        detailed_error = _get_detailed_error_from_tasks(
            backend,
            conversation_id,
            timeout_secs=_image_operation_timeout(request, 5.0),
            wait_secs=0.0,
        )
        error_text = detailed_error or message or "Image generation was rejected by upstream policy."
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text=error_text,
            upstream_event_type="content_policy",
            conversation_id=conversation_id,
        )
        return
    should_poll_for_image = stream_interrupted or bool(request.images) or last.get("turn_use_case") == "image gen"
    if message and not file_ids and not sediment_ids and not should_poll_for_image:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
    # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开，
    # 图片可能正在异步生成中。需要使用更积极的轮询策略来获取结果。
    is_text_reply = bool(message and is_model_text_reply_instead_of_image(message))
    if is_text_reply:
        logger.info({
            "event": "image_detected_text_reply_with_ids",
            "conversation_id": conversation_id,
            "message_preview": message[:200],
        })

    # 当检测到文本回复但 conversation_id 丢失时，尝试从最近对话列表中恢复
    # SSE 流太短时（模型返回文本而非触发图片工具），conversation_id 可能未被捕获，
    # 但图片已在上游异步生成。通过列出最近对话来恢复 conversation_id。
    if is_text_reply and not conversation_id:
        try:
            import time as _time
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt,
                _time.time(),
                timeout_secs=_image_operation_timeout(request, 5.0),
            )
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_conversation_id_recovered",
                    "conversation_id": conversation_id,
                    "message_preview": message[:200],
                })
        except ImagePollTimeoutError:
            raise
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_id_recovery_failed",
                "error": repr(exc)[:300],
            })

    # 在轮询图片之前，先检查 /backend-api/tasks/ 是否有 moderation 拦截
    # 这样可以避免不必要的长时间轮询超时
    # 注意：当 should_poll_for_image 为 True 或检测到文本回复时，
    # 即使 tasks 报告了"错误"，也不能直接返回——因为上游可能将工具调用的 JSON 参数
    # （如 {"size":"1792x1024","n":1}）标记为 is_error，而实际上图片正在异步生成中。
    # 此时应继续轮询图片。
    detailed_error = ""
    if not file_ids and not sediment_ids and conversation_id:
        detailed_error = _get_detailed_error_from_tasks(
            backend,
            conversation_id,
            timeout_secs=_image_operation_timeout(request, 5.0),
            wait_secs=0.0,
        )
        if detailed_error and not should_poll_for_image and not is_text_reply:
            logger.info({
                "event": "image_task_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
            return
        if detailed_error and (should_poll_for_image or is_text_reply):
            logger.info({
                "event": "image_task_error_skipped_for_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })

    # 当检测到文本回复（含 referenced_image_ids）时，使用更长的超时来轮询图片结果。
    # 因为上游可能将图片生成作为异步任务执行，SSE 流在工具完成前就断开了，
    # 导致对话文档中尚未写入图片工具的响应记录。
    poll_timeout = _remaining_image_poll_budget(request)
    if poll_timeout <= 0:
        raise ImagePollTimeoutError(_image_timeout_message(request))
    if is_text_reply and conversation_id:
        # 文本回复场景下图片可能仍在异步生成，使用更长超时（默认 120s → 额外 180s = 300s）
        logger.info({
            "event": "image_text_reply_extended_poll",
            "conversation_id": conversation_id,
            "poll_timeout_secs": poll_timeout,
        })

    try:
        if stream_interrupted and (file_ids or sediment_ids):
            # Generated IDs are already known: avoid spending the recovery
            # budget on another confirmation/settle poll before delivery.
            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
                poll_timeout_secs=poll_timeout,
            )
            if not image_urls:
                image_urls = backend.resolve_conversation_image_urls(
                    conversation_id, file_ids, sediment_ids,
                    poll_timeout_secs=_image_operation_timeout(request, poll_timeout),
                )
        else:
            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll_timeout_secs=poll_timeout,
            )
    except (ImageContentPolicyError, ImagePollTimeoutError) as exc:
        # 当检测到文本回复时，task error 不应直接判定为内容策略违规，
        # 因为图片可能仍在后台异步生成中
        if is_text_reply and isinstance(exc, ImageContentPolicyError):
            logger.warning({
                "event": "image_text_reply_task_error_ignored",
                "conversation_id": conversation_id,
                "error": str(exc),
            })
            image_urls = []
        else:
            raise
    except Exception as exc:
        # 当检测到文本回复时，首次轮询的临时网络错误不应直接中断，
        # 因为图片可能仍在后台异步生成中，后续 retry poll 会继续尝试。
        if is_text_reply and conversation_id:
            logger.warning({
                "event": "image_text_reply_first_poll_error_ignored",
                "conversation_id": conversation_id,
                "error": repr(exc)[:300],
            })
            image_urls = []
        else:
            raise

    if image_urls:
        if request.progress_callback:
            request.progress_callback("receiving_image")
        data = _download_and_format_generated_images(backend, image_urls, request)
        if data:
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
        return

    if message:
        # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
        # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开。
        # 此时应再尝试轮询图片结果，而不是直接把文本当作最终输出。
        # 当 is_text_reply 但 conversation_id 丢失时，尝试从最近对话列表恢复
        if is_text_reply and not conversation_id:
            try:
                import time as _time
                recovered_id = backend.find_conversation_by_prompt(
                    request.prompt,
                    _time.time(),
                    timeout_secs=_image_operation_timeout(request, 5.0),
                )
                if recovered_id:
                    conversation_id = recovered_id
                    logger.info({
                        "event": "image_text_reply_conversation_id_recovered",
                        "conversation_id": conversation_id,
                        "message_preview": message[:200],
                    })
            except ImagePollTimeoutError:
                raise
            except Exception as exc:
                logger.warning({
                "event": "image_text_reply_conversation_id_recovery_failed",
                    "error": repr(exc)[:300],
                })
        if is_text_reply and conversation_id:
            logger.info({
                "event": "image_model_text_reply_retry_poll",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
            # 文本回复场景下，图片可能需要 4-5 分钟才能异步生成完成。
            # 使用 300s 超时并允许多次重试，避免因临时网络问题提前退出。
            MAX_POLL_RETRIES = 3
            for poll_attempt in range(1, MAX_POLL_RETRIES + 1):
                retry_poll_timeout = _remaining_image_poll_budget(request)
                if retry_poll_timeout <= 0:
                    break
                try:
                    polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                        conversation_id,
                        retry_poll_timeout,
                        file_ids,
                        sediment_ids,
                    )
                    file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                    sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                    break  # 轮询成功，退出重试循环
                except Exception as exc:
                    error_str = str(exc)
                    is_transient = (
                        isinstance(exc, ImagePollTimeoutError)
                        or is_tls_connection_error(error_str)
                        or "upstream" in error_str.lower()
                        or "connection" in error_str.lower()
                        or "timeout" in error_str.lower()
                    )
                    logger.warning({
                        "event": "image_model_text_reply_poll_failed",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "error": repr(exc)[:300],
                        "is_transient": is_transient,
                    })
                    # 如果还有重试次数且不是超时/内容违规错误，继续重试
                    if poll_attempt < MAX_POLL_RETRIES and not isinstance(exc, (ImagePollTimeoutError, ImageContentPolicyError)):
                        # 递增退避：30s, 60s, 90s
                        backoff = 30.0 * poll_attempt
                        remaining = _remaining_image_poll_budget(request)
                        if remaining <= 0:
                            break
                        logger.info({
                            "event": "image_model_text_reply_poll_retry",
                            "conversation_id": conversation_id,
                            "poll_attempt": poll_attempt,
                            "backoff_secs": min(backoff, remaining),
                        })
                        time.sleep(min(backoff, remaining))
                        continue
                    # 超时错误或重试次数用尽，停止重试
                    break

            if file_ids or sediment_ids:
                _image_operation_timeout(request, 60.0)
                image_urls = backend.resolve_conversation_image_urls(
                    conversation_id, file_ids, sediment_ids, poll=False,
                )
                if image_urls:
                    if request.progress_callback:
                        request.progress_callback("receiving_image")
                    data = _download_and_format_generated_images(backend, image_urls, request)
                    if data:
                        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
                        return
        elif is_text_reply:
            logger.warning({
                "event": "image_model_text_reply_no_image",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    # 兜底：当 message 为空且图片 URL 解析失败时，先尝试一次短延迟重试轮询
    # 然后抛出明确错误而非让调用方得到 "upstream completed without generating images" 这种模糊报错
    logger.warning({
        "event": "image_stream_no_result_fallback",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "should_poll_for_image": should_poll_for_image,
    })
    # 当 should_poll_for_image 为 True 但 conversation_id 丢失时，尝试恢复
    if should_poll_for_image and not conversation_id:
        try:
            import time as _time
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt,
                _time.time(),
                timeout_secs=_image_operation_timeout(request, 5.0),
            )
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_fallback_conversation_id_recovered",
                    "conversation_id": conversation_id,
                })
        except ImagePollTimeoutError:
            raise
        except Exception as exc:
            logger.warning({
                "event": "image_fallback_conversation_id_recovery_failed",
                "error": repr(exc)[:300],
            })
    if should_poll_for_image and conversation_id:
        # 图片可能仍在异步处理中（上游 SSE 流在图片生成完成前就结束了）。
        # 使用 300s 超时并允许多次重试，避免因临时网络问题或图片尚未提交而提前退出。
        MAX_FALLBACK_POLL_RETRIES = 3
        for poll_attempt in range(1, MAX_FALLBACK_POLL_RETRIES + 1):
            remaining = _remaining_image_poll_budget(request)
            if remaining <= 0:
                break
            retry_wait_secs = min(
                30.0 * poll_attempt,
                config.image_poll_initial_wait_secs * poll_attempt,
                remaining,
            )
            logger.info({
                "event": "image_stream_retry_poll_after_wait",
                "conversation_id": conversation_id,
                "retry_wait_secs": retry_wait_secs,
                "poll_attempt": poll_attempt,
            })
            time.sleep(retry_wait_secs)
            retry_poll_timeout = _remaining_image_poll_budget(request)
            if retry_poll_timeout <= 0:
                break
            try:
                polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                    conversation_id,
                    retry_poll_timeout,
                    file_ids,
                    sediment_ids,
                )
                file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                break  # 轮询成功，退出重试循环
            except Exception as exc:
                error_str = str(exc)
                is_transient = (
                    isinstance(exc, ImagePollTimeoutError)
                    or is_tls_connection_error(error_str)
                    or "upstream" in error_str.lower()
                    or "connection" in error_str.lower()
                    or "timeout" in error_str.lower()
                )
                logger.warning({
                    "event": "image_stream_retry_poll_failed",
                    "conversation_id": conversation_id,
                    "poll_attempt": poll_attempt,
                    "error": repr(exc)[:300],
                    "is_transient": is_transient,
                })
                # 如果还有重试次数且不是超时/内容违规错误，继续重试
                if poll_attempt < MAX_FALLBACK_POLL_RETRIES and not isinstance(exc, (ImagePollTimeoutError, ImageContentPolicyError)):
                    # 递增退避：30s, 60s
                    backoff = 30.0 * poll_attempt
                    remaining = _remaining_image_poll_budget(request)
                    if remaining <= 0:
                        break
                    logger.info({
                        "event": "image_stream_retry_poll_retry",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "backoff_secs": min(backoff, remaining),
                    })
                    time.sleep(min(backoff, remaining))
                    continue
                # 超时错误或重试次数用尽，停止重试
                break
        
        if file_ids or sediment_ids:
            _image_operation_timeout(request, 60.0)
            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
            )
            if image_urls:
                if request.progress_callback:
                    request.progress_callback("receiving_image")
                data = _download_and_format_generated_images(backend, image_urls, request)
                if data:
                    yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
                    return
        
        # 重试后仍然失败，yield 错误消息
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation completed upstream but the result could not be retrieved. "
                               "The image may still be processing. Please try again in a moment.",
                          upstream_event_type="result_unavailable",
                          conversation_id=conversation_id)
    elif message:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
    else:
        # conversation_id 也为空时（SSE 流极短、未捕获到会话 ID），
        # 仍然 yield 一条消息，避免 stream_image_outputs_with_pool 产生
        # "upstream completed without generating images" 模糊报错
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation started upstream but the response was incomplete. "
                               "Please try again.",
                          conversation_id=conversation_id)


def _codex_response_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            if result:
                return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        images: list[str] = []
        for item in value.values():
            images.extend(_codex_response_images(item))
        return images
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_codex_response_images(item))
        return images
    return []


def stream_codex_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    timeout_secs = _image_stream_operation_timeout(request)
    images = _codex_response_images(list(backend.iter_codex_image_response_events(
        prompt=request.prompt,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
        timeout_secs=timeout_secs,
    )))
    if not images:
        raise ImageGenerationError("No image result found in response")
    try:
        data = format_image_result(
            [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
            request.prompt,
            request.response_format,
            request.base_url,
            int(time.time()),
        )["data"]
    except Exception as exc:
        raise ImageGenerationError(
            "image was generated but its result could not be retrieved",
            code=IMAGE_RESULT_POSTPROCESS_ERROR_CODE,
        ) from exc
    if data:
        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data)
        return
    raise ImageGenerationError("No image result found in response")


def _generate_single_image(
        request: ConversationRequest,
        index: int,
        total: int,
) -> list[ImageOutput]:
    """Generate one image and switch accounts only after a retryable failure."""
    request_deadline = request.image_request_deadline
    if request_deadline is None:
        # Keep direct callers bounded too. The pooled entry point normally
        # supplies this deadline once for all requested images.
        request_deadline = request.image_attempt_deadline or (
            time.monotonic() + config.image_request_timeout_secs
        )
        request = replace(request, image_request_deadline=request_deadline)

    plan_type, _ = split_image_model(request.model)
    codex_model = is_codex_image_model(request.model)
    max_attempts = config.image_max_account_attempts if config.image_account_retry_enabled else 1
    attempted_tokens: set[str] = set()
    last_error: ImageGenerationError | None = None

    for attempt in range(1, max_attempts + 1):
        remaining = request_deadline - time.monotonic()
        if remaining <= 0:
            raise ImageGenerationError(
                _image_request_timeout_message(),
                code="upstream_timeout",
                account_email=last_error.account_email if last_error is not None else "",
            )

        token = ""
        backend = None
        account_email = ""
        slot_finalized = False
        outputs: list[ImageOutput] = []
        retry_error: ImageGenerationError | None = None
        last_conversation_id = ""

        def finalize_image_slot(success: bool) -> None:
            nonlocal slot_finalized
            if not slot_finalized and token:
                # mark_image_result releases the reservation before persisting
                # counters. Mark it final first to prevent a second release if
                # persistence fails after the release.
                slot_finalized = True
                account_service.mark_image_result(token, success)

        def finalize_failed_attempt() -> None:
            try:
                finalize_image_slot(False)
            except Exception as finalize_error:
                logger.warning({
                    "event": "image_slot_finalize_failed",
                    "account_email": account_email,
                    "attempt": attempt,
                    "error": str(finalize_error),
                })

        try:
            if request.progress_callback:
                request.progress_callback("getting_account")
            preflight_timeout = min(remaining, IMAGE_ACCOUNT_PREFLIGHT_TIMEOUT_SECS)
            token = account_service.get_available_access_token(
                plan_type=plan_type,
                source_type="codex" if codex_model else None,
                plan_types=("plus", "team", "pro") if codex_model and not plan_type else None,
                excluded_tokens=attempted_tokens,
                timeout_secs=preflight_timeout,
            )
            attempted_tokens.add(token)
        except ImagePreflightTransportError as exc:
            # A TLS/connection failure during account validation is an
            # upstream-path failure, not evidence that every account in the
            # pool is unusable. AccountService has already released its
            # temporary reservation, so retry this image attempt with jitter
            # instead of scanning the rest of the account pool immediately.
            raw_error = str(exc.raw_error or exc)
            is_timeout = exc.is_timeout or is_connection_timeout_error(raw_error)
            retry_error = ImageGenerationError(
                "upstream connection timed out, please retry later" if is_timeout else image_stream_error_message(raw_error),
                code="upstream_timeout" if is_timeout else "upstream_error",
                raw_error=raw_error,
                stage="getting_account",
                attempt=attempt,
            )
            logger.warning({
                "event": "image_upstream_transport_failure",
                "stage": "getting_account",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "index": index,
                "total": total,
                "account_email": "",
                "conversation_id": "",
                "error_type": type(exc).__name__,
                "raw_error": raw_error[:1000],
            })
            last_error = retry_error
            if request_deadline - time.monotonic() <= 0:
                raise ImageGenerationError(
                    _image_request_timeout_message(),
                    code="upstream_timeout",
                ) from exc
            if attempt >= max_attempts:
                _monitor_image_stage(
                    request,
                    "account_retries_exhausted",
                    error=str(retry_error),
                    attempt=attempt,
                    max_attempts=max_attempts,
                    index=index,
                    total=total,
                )
                raise retry_error from exc
            backoff_secs = _image_transport_retry_backoff_secs(attempt, request_deadline)
            logger.warning({
                "event": "image_upstream_transport_retry_backoff",
                "stage": "getting_account",
                "attempt": attempt,
                "next_attempt": attempt + 1,
                "max_attempts": max_attempts,
                "index": index,
                "total": total,
                "account_email": "",
                "conversation_id": "",
                "raw_error": raw_error[:1000],
                "backoff_secs": backoff_secs,
                "remaining_secs": max(0.0, request_deadline - time.monotonic()),
            })
            _monitor_image_stage(
                request,
                "transport_retry_backoff",
                error=raw_error,
                retry_count=attempt,
                backoff_secs=backoff_secs,
                index=index,
                total=total,
            )
            if backoff_secs > 0:
                time.sleep(backoff_secs)
            logger.warning({
                "event": "image_account_retry",
                "account_email": "",
                "retry_count": attempt,
                "max_attempts": max_attempts,
                "index": index,
                "error": str(retry_error)[:240],
            })
            _monitor_image_stage(
                request,
                "retrying_account",
                previous_account_email="",
                error=str(retry_error),
                retry_count=attempt,
                attempt=attempt,
                max_attempts=max_attempts,
                index=index,
                total=total,
            )
            continue
        except RuntimeError as exc:
            if request_deadline - time.monotonic() <= 0:
                raise ImageGenerationError(
                    _image_request_timeout_message(),
                    code="upstream_timeout",
                    account_email=last_error.account_email if last_error is not None else "",
                ) from exc
            if last_error is not None:
                raise last_error from exc
            raise ImageGenerationError(
                str(exc) or "image generation failed",
                raw_error=str(exc), stage="getting_account", attempt=attempt,
            ) from exc

        try:
            account = account_service.get_account(token) or {}
            account_email = str(account.get("email") or "").strip()
            _monitor_image_stage(
                request,
                "account_selected",
                account_email=account_email,
                attempt=attempt,
                max_attempts=max_attempts,
                index=index,
                total=total,
            )
            logger.debug({
                "event": "image_account_lookup",
                "account_email": account_email,
                "account_found": bool(account),
                "attempt": attempt,
                "index": index,
            })
            backend = OpenAIBackendAPI(access_token=token)
            _monitor_image_stage(
                request,
                "starting_generation",
                account_email=account_email,
                attempt=attempt,
                max_attempts=max_attempts,
                index=index,
                total=total,
            )
            if request.progress_callback:
                backend.progress_callback = request.progress_callback
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise ImageGenerationError(
                    _image_request_timeout_message(),
                    code="upstream_timeout",
                    account_email=account_email,
                )
            attempts_left = max_attempts - attempt + 1
            # Reserve enough time for one upstream stream and one result poll
            # per account, while still splitting the shared request budget
            # fairly between possible account retries.
            attempt_budget = min(
                float(config.image_stream_timeout_secs + config.image_poll_timeout_secs),
                remaining / attempts_left,
            )
            attempt_request = replace(
                request,
                image_attempt_deadline=min(request_deadline, time.monotonic() + attempt_budget),
            )
            stream_fn = stream_codex_image_outputs if codex_model else stream_image_outputs
            try:
                for output in stream_fn(backend, attempt_request, index, total):
                    last_conversation_id = output.conversation_id or last_conversation_id
                    if account_email and not output.account_email:
                        output.account_email = account_email
                    outputs.append(output)
            except Exception as exc:
                last_conversation_id = last_conversation_id or str(
                    getattr(exc, "conversation_id", "") or ""
                )
                if last_conversation_id and not getattr(exc, "conversation_id", ""):
                    # The SSE stream may have yielded an ID before result
                    # polling timed out. Keep it available for resume-poll.
                    try:
                        setattr(exc, "conversation_id", last_conversation_id)
                    except Exception:
                        pass
                raise
            finally:
                _remove_image_conversation_later(
                    backend,
                    last_conversation_id,
                    success=any(output.kind == "result" for output in outputs),
                )

            if any(output.kind == "result" for output in outputs):
                try:
                    finalize_image_slot(True)
                except Exception as finalize_error:
                    # The upstream result has already been produced. Do not
                    # create a duplicate image just because account accounting
                    # failed after releasing its slot.
                    logger.warning({
                        "event": "image_slot_finalize_failed_after_result",
                        "account_email": account_email,
                        "attempt": attempt,
                        "error": str(finalize_error),
                    })
                _monitor_image_stage(
                    request,
                    "image_ready",
                    account_email=account_email,
                    conversation_id=next((output.conversation_id for output in reversed(outputs) if output.conversation_id), ""),
                    attempt=attempt,
                    max_attempts=max_attempts,
                    index=index,
                    total=total,
                )
                return outputs

            message_output = next((output for output in reversed(outputs) if output.kind == "message"), None)
            conversation_id = next((output.conversation_id for output in reversed(outputs) if output.conversation_id), "")
            if message_output is not None and is_image_quota_error_text(message_output.text):
                raise ImageGenerationError(
                    message_output.text,
                    status_code=429,
                    error_type="insufficient_quota",
                    code="image_quota_exhausted",
                    account_email=account_email,
                    conversation_id=conversation_id,
                )
            if message_output is not None and message_output.upstream_event_type == "content_policy":
                raise ImageGenerationError(
                    message_output.text or "Image generation was rejected by upstream policy.",
                    status_code=400,
                    error_type="invalid_request_error",
                    code="content_policy_violation",
                    account_email=account_email,
                    conversation_id=conversation_id,
                )
            if message_output is not None and message_output.upstream_event_type == "result_unavailable":
                raise ImageGenerationError(
                    message_output.text or "image was generated but its result could not be retrieved",
                    code="upstream_result_unavailable",
                    account_email=account_email,
                    conversation_id=conversation_id,
                )
            if message_output is not None and not request.message_as_error:
                try:
                    finalize_image_slot(False)
                except Exception as finalize_error:
                    logger.warning({
                        "event": "image_slot_finalize_failed_after_message",
                        "account_email": account_email,
                        "attempt": attempt,
                        "error": str(finalize_error),
                    })
                _monitor_image_stage(
                    request,
                    "upstream_message",
                    account_email=account_email,
                    conversation_id=conversation_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    index=index,
                    total=total,
                )
                return outputs
            raise ImageGenerationError(
                message_output.text if message_output is not None and message_output.text else "upstream completed without generating images",
                code="no_image_generated",
                account_email=account_email,
                conversation_id=conversation_id,
            )
        except ImageContentPolicyError as exc:
            finalize_failed_attempt()
            _monitor_image_stage(
                request,
                "rejected",
                account_email=account_email,
                code="content_policy_violation",
                error=str(exc),
                attempt=attempt,
                max_attempts=max_attempts,
                index=index,
                total=total,
            )
            raise ImageGenerationError(
                str(exc) or "Image generation was rejected by upstream policy.",
                status_code=400,
                error_type="invalid_request_error",
                code="content_policy_violation",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
            ) from exc
        except ImageStreamHardTimeoutError as exc:
            # The local SSE watchdog only proves this request exceeded its
            # time budget. Do not treat it as an account failure; release the
            # reservation in finally and give another account a chance.
            retry_error = ImageGenerationError(
                str(exc) or "image generation stream timed out",
                code="upstream_timeout",
                account_email=account_email,
                conversation_id=str(getattr(exc, "conversation_id", "") or last_conversation_id),
                raw_error=str(exc),
                stage="image_stream",
                attempt=attempt,
            )
        except Exception as exc:
            # Persist the quota deadline before releasing this account's slot.
            # This also handles a quota rejection delivered inside a 200 SSE response.
            quota_detail = getattr(exc, "body", None) or getattr(exc, "raw_error", "") or str(exc)
            if is_image_quota_error_text(quota_detail):
                account_service.mark_image_quota_exhausted(
                    token,
                    restore_at=extract_image_quota_restore_at(quota_detail),
                    error=str(exc),
                )
            finalize_failed_attempt()
            error_text = str(exc)
            conversation_id = str(getattr(exc, "conversation_id", "") or last_conversation_id)
            raw_error = str(getattr(exc, "raw_error", "") or error_text)
            is_transport_error = _is_image_transport_error(raw_error)
            if is_transport_error:
                logger.warning({
                    "event": "image_upstream_transport_failure",
                    "stage": "image_stream",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "index": index,
                    "total": total,
                    "account_email": account_email,
                    "conversation_id": conversation_id,
                    "error_type": type(exc).__name__,
                    "raw_error": raw_error[:1000],
                })
            if (
                isinstance(exc, UpstreamHTTPError)
                and exc.status_code in DETERMINISTIC_IMAGE_REQUEST_STATUS_CODES
                and not (is_exact_image_model_not_found(exc) and _is_auto_image_model(request.model))
            ):
                _monitor_image_stage(
                    request,
                    "rejected",
                    account_email=account_email,
                    code="upstream_client_error",
                    error=error_text,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    index=index,
                    total=total,
                )
                raise ImageGenerationError(
                    image_stream_error_message(error_text),
                    status_code=exc.status_code,
                    error_type="invalid_request_error",
                    code="upstream_client_error",
                    account_email=account_email,
                    conversation_id=conversation_id,
                    raw_error=raw_error,
                    stage="image_stream",
                    attempt=attempt,
                ) from exc
            if is_exact_image_model_not_found(exc) and _is_auto_image_model(request.model):
                # Exact "Model not found" from an actual auto image session:
                # cool the account locally and let the remaining retry budget
                # switch to another account.
                try:
                    account_service.mark_image_model_not_found(token, error=error_text)
                except Exception as cooldown_error:
                    logger.warning({
                        "event": "image_model_not_found_cooldown_failed",
                        "account_email": account_email,
                        "attempt": attempt,
                        "error": str(cooldown_error),
                    })
                _monitor_image_stage(
                    request,
                    "model_not_found_cooldown",
                    account_email=account_email,
                    code="image_model_not_found",
                    error=error_text,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    index=index,
                    total=total,
                )
                retry_error = ImageGenerationError(
                    image_stream_error_message(error_text),
                    status_code=404,
                    error_type="invalid_request_error",
                    code="image_model_not_found",
                    account_email=account_email,
                    conversation_id=conversation_id,
                    raw_error=raw_error,
                    stage="image_stream",
                    attempt=attempt,
                )
            if isinstance(exc, ImageGenerationError):
                if account_email and not exc.account_email:
                    exc.account_email = account_email
                if conversation_id and not exc.conversation_id:
                    exc.conversation_id = conversation_id
                if raw_error and not exc.raw_error:
                    exc.raw_error = raw_error
                if not exc.stage:
                    exc.stage = "image_stream"
                if not exc.attempt:
                    exc.attempt = attempt
                if not _is_retryable_image_failure(exc):
                    _monitor_image_stage(
                        request,
                        "rejected",
                        account_email=exc.account_email,
                        code=exc.code or "",
                        error=error_text,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        index=index,
                        total=total,
                    )
                    raise
                retry_error = exc
            elif not _is_retryable_image_failure(exc):
                _monitor_image_stage(
                    request,
                    "failed",
                    account_email=account_email,
                    error=error_text,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    index=index,
                    total=total,
                )
                raise ImageGenerationError(
                    image_stream_error_message(error_text),
                    account_email=account_email,
                    conversation_id=conversation_id,
                    raw_error=raw_error,
                    stage="image_stream",
                    attempt=attempt,
                ) from exc
            else:
                if is_token_invalid_error(error_text):
                    refresh_timeout = request_deadline - time.monotonic()
                    if refresh_timeout <= 0:
                        retry_error = ImageGenerationError(
                            _image_request_timeout_message(),
                            code="upstream_timeout",
                            account_email=account_email,
                        )
                    else:
                        try:
                            refreshed_token = account_service.refresh_access_token(
                                token,
                                force=True,
                                event="image_stream",
                                timeout_secs=refresh_timeout,
                            )
                        except TimeoutError as refresh_exc:
                            retry_error = ImageGenerationError(
                                _image_request_timeout_message(),
                                code="upstream_timeout",
                                account_email=account_email,
                            )
                            logger.warning({
                                "event": "image_token_refresh_timed_out",
                                "account_email": account_email,
                                "attempt": attempt,
                                "error": str(refresh_exc),
                            })
                        else:
                            if refreshed_token:
                                attempted_tokens.add(refreshed_token)
                            if not refreshed_token or refreshed_token == token:
                                account_service.remove_invalid_token(token, "image_stream")
                if retry_error is None:
                    retry_error = ImageGenerationError(
                        image_stream_error_message(error_text),
                        code="upstream_timeout" if isinstance(exc, ImagePollTimeoutError) else "upstream_error",
                        account_email=account_email,
                        conversation_id=conversation_id,
                        raw_error=raw_error,
                        stage="image_stream",
                        attempt=attempt,
                    )
        finally:
            if token and not slot_finalized:
                account_service.release_image_slot(token)
            if backend is not None:
                try:
                    backend.close()
                except Exception as close_error:
                    logger.warning({
                        "event": "image_backend_close_failed",
                        "account_email": account_email,
                        "attempt": attempt,
                        "error": str(close_error),
                    })

        if retry_error is None:
            _monitor_image_stage(
                request,
                "failed",
                account_email=account_email,
                attempt=attempt,
                max_attempts=max_attempts,
                index=index,
                total=total,
            )
            raise ImageGenerationError("image generation failed", account_email=account_email)
        last_error = retry_error
        if request_deadline - time.monotonic() <= 0:
            _monitor_image_stage(
                request,
                "request_timeout",
                account_email=account_email,
                error=str(retry_error),
                attempt=attempt,
                max_attempts=max_attempts,
                index=index,
                total=total,
            )
            raise ImageGenerationError(
                _image_request_timeout_message(),
                code="upstream_timeout",
                account_email=account_email,
            ) from retry_error
        if attempt >= max_attempts:
            _monitor_image_stage(
                request,
                "account_retries_exhausted",
                account_email=account_email,
                error=str(retry_error),
                attempt=attempt,
                max_attempts=max_attempts,
                index=index,
                total=total,
            )
            raise retry_error
        if _is_image_transport_error(retry_error):
            backoff_secs = _image_transport_retry_backoff_secs(attempt, request_deadline)
            raw_error = str(retry_error.raw_error or retry_error)
            logger.warning({
                "event": "image_upstream_transport_retry_backoff",
                "stage": retry_error.stage or "image_stream",
                "attempt": attempt,
                "next_attempt": attempt + 1,
                "max_attempts": max_attempts,
                "index": index,
                "total": total,
                "account_email": retry_error.account_email or account_email,
                "conversation_id": retry_error.conversation_id,
                "raw_error": raw_error[:1000],
                "backoff_secs": backoff_secs,
                "remaining_secs": max(0.0, request_deadline - time.monotonic()),
            })
            _monitor_image_stage(
                request,
                "transport_retry_backoff",
                account_email=retry_error.account_email or account_email,
                conversation_id=retry_error.conversation_id,
                error=raw_error,
                retry_count=attempt,
                backoff_secs=backoff_secs,
                index=index,
                total=total,
            )
            if backoff_secs > 0:
                time.sleep(backoff_secs)
        logger.warning({
            "event": "image_account_retry",
            "account_email": account_email,
            "retry_count": attempt,
            "max_attempts": max_attempts,
            "index": index,
            "error": str(retry_error)[:240],
        })
        _monitor_image_stage(
            request,
            "retrying_account",
            account_email=account_email,
            previous_account_email=account_email,
            error=str(retry_error),
            retry_count=attempt,
            attempt=attempt,
            max_attempts=max_attempts,
            index=index,
            total=total,
        )

    raise last_error or ImageGenerationError("image generation failed")


def stream_image_outputs_with_pool(request: ConversationRequest) -> Iterator[ImageOutput]:
    """并行生成多张图片，每张图片使用独立线程和账号，互不阻塞。"""
    if not is_supported_image_model(request.model):
        raise ImageGenerationError("unsupported image model,supported models: " + ", ".join(sorted(IMAGE_MODELS)))

    if request.image_request_deadline is None:
        request = replace(
            request,
            image_request_deadline=time.monotonic() + config.image_request_timeout_secs,
        )

    if request.n <= 1:
        # 单张图片，直接执行（无需线程池开销）
        outputs = _generate_single_image(request, 1, 1)
        for output in outputs:
            yield output
        return

    # 多张图片：根据配置选择并行或串行执行
    if not config.image_parallel_generation:
        logger.info({
            "event": "image_serial_generation_start",
            "n": request.n,
            "model": request.model,
        })
        for index in range(1, request.n + 1):
            outputs = _generate_single_image(request, index, request.n)
            for output in outputs:
                yield output
        return

    logger.info({
        "event": "image_parallel_generation_start",
        "n": request.n,
        "model": request.model,
    })
    # 每张图片一个线程，同时启动
    futures = {}
    results: dict[int, list[ImageOutput]] = {}
    errors: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=request.n) as executor:
        for index in range(1, request.n + 1):
            future = executor.submit(_generate_single_image, request, index, request.n)
            futures[future] = index

        # 按完成顺序收集结果
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                errors[index] = exc
                logger.warning({
                    "event": "image_parallel_generation_error",
                    "index": index,
                    "error": str(exc)[:300],
                })

    # yield 结果：跳过索引顺序限制，不再让低索引失败阻塞高索引成功结果
    emitted = False
    last_error: Exception | None = None
    # 先 yield 所有成功的结果
    for index in range(1, request.n + 1):
        if index in results:
            for output in results[index]:
                emitted = True
                yield output
        elif index in errors:
            last_error = errors[index]
            if not emitted:
                logger.warning({
                    "event": "image_parallel_failure_before_success",
                    "failed_index": index,
                    "error": str(last_error)[:200],
                })

    # 如果有失败但也有成功，记录警告
    if emitted:
        for index in range(1, request.n + 1):
            if index in errors:
                logger.warning({
                    "event": "image_parallel_partial_failure",
                    "failed_index": index,
                    "error": str(errors[index])[:200],
                })

    if not emitted:
        if not last_error:
            last_error = "no account in the pool could generate images — check account quota and rate-limit status"
        image_errors = [
            error
            for _, error in sorted(errors.items())
            if isinstance(error, ImageGenerationError)
        ]
        timeout_error = next(
            (error for error in image_errors if error.code == "image_model_not_found"),
            next((error for error in image_errors if error.code == "upstream_timeout"), None),
        )
        if timeout_error is not None:
            raise timeout_error
        if image_errors:
            raise image_errors[-1]
        raise ImageGenerationError(image_stream_error_message(str(last_error)), conversation_id="")


def stream_image_chunks(outputs: Iterable[ImageOutput]) -> Iterator[dict[str, Any]]:
    for output in outputs:
        yield output.to_chunk()


def collect_image_outputs(outputs: Iterable[ImageOutput]) -> dict[str, Any]:
    created = None
    data: list[dict[str, Any]] = []
    message = ""
    progress_parts: list[str] = []
    account_email = ""
    for output in outputs:
        created = created or output.created
        if output.account_email and not account_email:
            account_email = output.account_email
        if output.kind == "progress" and output.text:
            progress_parts.append(output.text)
        elif output.kind == "message":
            message = output.text
        elif output.kind == "result":
            data.extend(output.data)

    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if not data:
        text = message or "".join(progress_parts).strip()
        if text:
            result["message"] = text
    if account_email:
        result["_account_email"] = account_email
    return result
