"""Adapter selection, the HTTP call, outcome classification, model discovery.

The httpx client is a parameter, never a module global and never built here.
The caller passes `app.state.notify_client`. `MtopClient`'s client is not an
option: it carries the goofish cookie jar, so using it would send the user's
logged-in session to a third-party endpoint. This module cannot reach it —
`llm/` imports nothing from `app/collector/`.
"""

import json
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.llm.anthropic import AnthropicAdapter
from app.llm.base import (
    EMPTY_MESSAGE,
    STARVED_MESSAGE,
    UNPARSABLE_MESSAGE,
    LlmAdapter,
    LlmConfigError,
    LlmError,
    LlmOutcome,
    LlmRequest,
    LlmResponse,
    LlmTransportError,
)
from app.llm.openai import OpenAiAdapter
from app.models import LlmEndpoint

log = logging.getLogger(__name__)

# A dict, not a plugin loader — two formats do not need discovery.
ADAPTERS: dict[str, LlmAdapter] = {
    OpenAiAdapter.wire_format: OpenAiAdapter(),
    AnthropicAdapter.wire_format: AnthropicAdapter(),
}

# Overrides the shared client's 20 s default: a reasoning model routinely
# thinks for a minute before the first byte, and 20 s would report a timeout
# for a request that was working.
CHAT_TIMEOUT_SECONDS = 120.0
# Listing models is a static lookup; waiting two minutes for it only delays
# the "type the name yourself" fallback.
DISCOVERY_TIMEOUT_SECONDS = 15.0

# Enough of the body to carry the real cause, short enough not to be a log dump.
BODY_CHARS = 200


def adapter_for(wire_format: str) -> LlmAdapter:
    """Raises LlmConfigError for a format we do not speak."""
    try:
        return ADAPTERS[wire_format]
    except KeyError as exc:
        raise LlmConfigError(
            f"不支持的 wire_format：{wire_format}（可选：{'、'.join(ADAPTERS)}）"
        ) from exc


async def complete(
    client: httpx.AsyncClient,
    endpoint: LlmEndpoint,
    model: str,
    request: LlmRequest,
) -> LlmResponse:
    """One completion. Raises LlmConfigError or LlmTransportError."""
    adapter = adapter_for(endpoint.wire_format)
    started = time.monotonic()
    try:
        response = await client.post(
            adapter.chat_url(endpoint.base_url),
            headers=adapter.headers(endpoint.api_key),
            json=adapter.body(model, request),
            timeout=CHAT_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        raise LlmTransportError(
            f"等待端点响应超时（超过 {CHAT_TIMEOUT_SECONDS:.0f} 秒）。推理模型较慢，"
            "可换更快的模型或稍后重试。"
        ) from exc
    except httpx.HTTPError as exc:
        # Deliberately drops str(exc): httpx puts the request URL in it, and a
        # user-configured base_url can carry the key in a query parameter.
        raise LlmTransportError("无法连接到 LLM 端点，请检查 base_url 与网络。") from exc

    if response.status_code != 200:
        raise LlmTransportError(_status_message(response, endpoint.api_key))

    payload = _payload(response)
    completion_tokens, reasoning_tokens = adapter.usage(payload)
    result = LlmResponse(
        text=adapter.text(payload),
        raw=payload,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
    )
    log.info(
        "llm call ok",
        extra={
            # No key, no prompt, no answer: the counts are what a cost or a
            # starvation question needs, and the rest is user content.
            "model": model,
            "wire_format": endpoint.wire_format,
            "completion_tokens": result.completion_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "chars": len(result.text),
            "ms": int((time.monotonic() - started) * 1000),
        },
    )
    return result


async def complete_structured(
    client: httpx.AsyncClient,
    endpoint: LlmEndpoint,
    model: str,
    request: LlmRequest,
    schema: type[BaseModel],
) -> LlmOutcome:
    """A completion validated against `schema`, with one retry.

    Three outcomes, never an exception for an HTTP 200: `ok`, `starved` (the
    budget went to reasoning — the fix is max_tokens, NOT the prompt), and
    `unparsable` (the model's own text is returned for display, because a user
    breaking the template is the normal case).

    Raises LlmConfigError / LlmTransportError only when no answer arrived.
    """
    first = await complete(client, endpoint, model, request)
    outcome = _classify(first, schema)
    if outcome.kind != "unparsable":
        return outcome

    # Once, not a loop: sampling is nondeterministic so a second draw often
    # parses, while a third spends the user's money for a shrinking chance.
    second = await complete(client, endpoint, model, request)
    retried = _classify(second, schema)
    if retried.kind != "unparsable":
        return retried

    log.warning(
        "llm output failed validation twice",
        extra={"model": model, "schema": schema.__name__, "chars": len(second.text)},
    )
    return LlmOutcome(
        kind="unparsable",
        text=second.text or first.text,
        message=UNPARSABLE_MESSAGE,
    )


async def discover_models(
    client: httpx.AsyncClient,
    endpoint: LlmEndpoint,
) -> tuple[list[str], str | None]:
    """Available model ids, or an empty list and a safe reason.

    Never raises. Plenty of relay gateways do not implement this route at all,
    and the answer to a failure is letting the user type the model name — not
    a 4xx that reads like the endpoint is broken.
    """
    try:
        adapter = adapter_for(endpoint.wire_format)
        response = await client.get(
            adapter.models_url(endpoint.base_url),
            headers=adapter.headers(endpoint.api_key),
            timeout=DISCOVERY_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return [], _status_message(response, endpoint.api_key)
        return adapter.models(_payload(response)), None
    except httpx.TimeoutException:
        return [], f"拉取模型列表超过 {DISCOVERY_TIMEOUT_SECONDS:.0f} 秒，可手动填写模型名。"
    except httpx.HTTPError:
        return [], "无法连接到 LLM 端点，请检查 base_url 与网络。"
    except LlmError as exc:
        # Our own family only: its messages are secret-free by construction.
        return [], str(exc)


def _classify(response: LlmResponse, schema: type[BaseModel]) -> LlmOutcome:
    if response.starved:
        return LlmOutcome(kind="starved", text=response.text, message=STARVED_MESSAGE)
    if response.empty:
        return LlmOutcome(kind="empty", text=response.text, message=EMPTY_MESSAGE)
    data = _parse(response.text, schema)
    if data is not None:
        return LlmOutcome(kind="ok", text=response.text, data=data)
    return LlmOutcome(kind="unparsable", text=response.text, message=UNPARSABLE_MESSAGE)


def _parse(text: str, schema: type[BaseModel]) -> dict[str, Any] | None:
    """Validated dict, or None if the model did not produce our JSON.

    No vendor structured-output parameter is used: the two wire formats do not
    offer an equivalent one, so the schema is stated in the prompt and
    enforced here.
    """
    try:
        payload = json.loads(_unfence(text))
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return schema.model_validate(payload).model_dump()
    except ValidationError:
        return None


def _unfence(text: str) -> str:
    """Strip a ```json fence.

    Chat models add one even when told not to, and a retry would just add it
    again — so this is the difference between one retry that helps and two
    calls that both fail.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped[3:]
    if body.startswith("json"):
        body = body[4:]
    return body.removesuffix("```").strip()


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LlmTransportError(
            "端点返回的不是 JSON，可能 base_url 指向了网页而不是 API。"
        ) from exc
    if not isinstance(payload, dict):
        raise LlmTransportError("端点返回的 JSON 不是对象，无法当作模型响应解析。")
    return payload


def _status_message(response: httpx.Response, api_key: str) -> str:
    """A user-facing reason for a non-200, with the key scrubbed out."""
    code = response.status_code
    if code in (401, 403):
        reason = "端点拒绝了认证，请检查 api_key"
    elif code == 404:
        reason = "端点路径不存在，请检查 base_url 与 wire_format"
    elif code == 429:
        reason = "端点限流，请稍后再试"
    elif code >= 500:
        reason = "端点内部错误"
    else:
        reason = "端点拒绝了请求"
    # The body is where the actual cause lives ("model not found",
    # "insufficient balance") and the status code alone sends the user to the
    # wrong place. Scrubbed because a gateway can echo the auth header back.
    body = _scrub(response.text[:BODY_CHARS], api_key)
    return f"{reason}（HTTP {code}）：{body}" if body else f"{reason}（HTTP {code}）"


def _scrub(text: str, api_key: str) -> str:
    """Remove the one secret we know is in flight.

    Redaction happens here, at the point of use, rather than in a filter
    someone has to remember to install (`spec/backend/logging-guidelines.md`).
    """
    return text.replace(api_key, "***") if api_key else text
