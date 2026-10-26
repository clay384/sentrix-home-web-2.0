"""领域错误 → HTTP 状态。所有 v1 端点共用同一结构。

**映射只有这一处。** 用例抛出**带码**的错误（基类在 ``domain/errors.py``，理由写在
那里），路由把它们原样放行；这个处理器是唯一把码翻成状态与响应体的地方。路由里出现
第二个映射点时，「状态码从哪来」就又变成看运气了。

本文件里的四个子类是 **api 层自己**在边界上发现的问题（找不到 asset、目录不存在、
bbox 非法）：它们不是用例的结论，而是请求本身在这里就没法继续。用同一族类型而不是
``HTTPException``，是为了让**所有**领域错误的响应形状只有一种。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, cast

from fastapi import Request
from fastapi.responses import JSONResponse

from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.domain.errors import DomainError


class NotFoundError(DomainError):
    code = "not_found"
    status_code = 404


class InvalidRequestError(DomainError):
    code = "invalid_request"
    status_code = 400


class ConflictError(DomainError):
    code = "conflict"
    status_code = 409


class UnprocessableError(DomainError):
    """请求语法没问题、内容无法处理（1.0 的人脸裁剪端点在框非法时用的就是 422）。"""

    code = "unprocessable"
    status_code = 422


async def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    # Starlette 的 add_exception_handler 要求第二参数是 Exception（函数参数逆变），
    # 而这里注册的就是 DomainError；cast 零开销，运行时要拿到的仍然是 DomainError。
    error = cast(DomainError, exc)
    return JSONResponse(
        status_code=error.status_code,
        content={"code": error.code, "message": error.message, "detail": {}},
    )


_OPERATIONAL_ERRORS = {
    "time_limit": (504, "模型请求超时；本次操作未完成。"),
    "provider_unavailable": (503, "模型服务不可用；本次操作未完成。"),
    "provider_rejected": (502, "模型服务拒绝请求；本次操作未完成。"),
    "context_limit": (422, "输入超出模型可用上下文；本次操作未完成。"),
    "output_limit": (502, "模型输出达到可用上限；本次操作未完成。"),
    "provider_response_invalid": (502, "模型服务响应封装不可识别；本次操作未完成。"),
    "model_output_invalid": (502, "模型输出格式错误；本次操作未完成。"),
    "request_configuration_invalid": (500, "请求配置错误；本次操作未派发模型请求。"),
}


def _safe_request_policy(policy: Mapping[str, Any]) -> dict[str, object]:
    # Only known execution parameters may leave the server. Never echo free
    # text, endpoint credentials, prompts, images or provider error bodies.
    safe: dict[str, object] = {}
    numeric_keys = {
        "max_model_len",
        "max_output_tokens",
        "request_timeout_s",
        "timeout_s",
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "repetition_penalty",
        "seed",
    }
    for key in numeric_keys:
        value = policy.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            safe[key] = value
    for key in ("output_budget", "output_budget_policy"):
        value = policy.get(key)
        if value in ("native_remaining_context", "estimated_remaining_context"):
            safe[key] = value
    if type(policy.get("request_outcome_unknown")) is bool:
        safe["request_outcome_unknown"] = policy["request_outcome_unknown"]
    if policy.get("max_output_tokens", "absent") is None:
        safe["max_output_tokens"] = None
    chat = policy.get("chat_template_kwargs")
    if isinstance(chat, dict) and type(chat.get("enable_thinking")) is bool:
        safe["chat_template_kwargs"] = {"enable_thinking": chat["enable_thinking"]}
    if policy.get("json_output_mode") in ("json_object", "prompt_only", "json_schema"):
        safe["json_output_mode"] = policy["json_output_mode"]
    if policy.get("request_phase") in ("request_validation", "model_discovery"):
        safe["request_phase"] = policy["request_phase"]
    name = policy.get("json_schema_name")
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        safe["json_schema_name"] = name
    checksum = policy.get("json_schema_sha256")
    if isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum):
        safe["json_schema_sha256"] = checksum
    if policy.get("json_schema_hash_format") == "sorted_compact_ascii_json_v1":
        safe["json_schema_hash_format"] = policy["json_schema_hash_format"]
    response_format = policy.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in ("json_object", "json_schema"):
        safe["response_format"] = {"type": response_format["type"]}
    return safe


async def model_service_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    error = cast(ModelServiceError, exc)
    code = error.error_code if error.error_code in _OPERATIONAL_ERRORS else "provider_unavailable"
    status, message = _OPERATIONAL_ERRORS[code]
    detail: dict[str, object] = {
        "error_code": code,
        "request_policy": _safe_request_policy(error.request_policy),
    }
    if type(error.http_status) is int and 100 <= error.http_status <= 599:
        detail["http_status"] = error.http_status
    if error.finish_reason in ("stop", "length", "tool_calls", "content_filter"):
        detail["finish_reason"] = error.finish_reason
    for key in ("prompt_tokens", "completion_tokens"):
        value = getattr(error, key)
        if type(value) is int and value >= 0:
            detail[key] = value
    return JSONResponse(status_code=status, content={"code": code, "message": message, "detail": detail})
