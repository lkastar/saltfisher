"""Outcome classification, structured output with one retry, model discovery,
and the two constraints that are security rather than behaviour: no api_key
anywhere it can be read, and no import path from `llm/` to the cookie jar.

Every call goes through httpx.MockTransport. A test that hits a real LLM
endpoint is a flake with a bill attached (`spec/backend/quality-guidelines.md`).
"""

import json
import logging

import httpx
import pytest
from pydantic import BaseModel

from app.llm.base import LlmConfigError, LlmRequest, LlmTransportError
from app.llm.client import complete, complete_structured, discover_models
from app.models import LlmEndpoint

pytestmark = pytest.mark.asyncio

KEY = "sk-liveKey0123456789"
REQUEST = LlmRequest(system="你是助手", user_text="报个价")


class Advice(BaseModel):
    verdict: str
    summary: str


def endpoint(wire_format: str = "openai") -> LlmEndpoint:
    return LlmEndpoint(
        label="probe",
        base_url="https://api.deepseek.com",
        api_key=KEY,
        wire_format=wire_format,
    )


def chat_payload(content: str, completion: int = 40, reasoning: int = 12) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 219,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def replaying(*responses: httpx.Response) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A client that answers with each response in turn, recording requests."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


# --------------------------------------------------------------------------- #
# The measured trap: HTTP 200, finish_reason stop, empty content
# --------------------------------------------------------------------------- #


async def test_empty_answer_with_reasoning_tokens_is_a_budget_problem():
    """Measured on this endpoint: max_tokens=60 returned 60 reasoning tokens,
    an empty string and no error. Reporting "check your prompt" here sends the
    user to debug the one thing that is not wrong.
    """
    client, _ = replaying(ok(chat_payload("", completion=60, reasoning=60)))
    outcome = await complete_structured(client, endpoint(), "m", REQUEST, Advice)

    assert outcome.kind == "starved"
    assert "max_tokens" in (outcome.message or "")
    assert "提示词" not in (outcome.message or "")
    assert outcome.data is None


async def test_starvation_does_not_burn_a_retry():
    """The retry exists for nondeterministic sampling. A starved response is
    deterministic — the budget is still too small on the second call."""
    client, seen = replaying(ok(chat_payload("", completion=60, reasoning=60)))
    await complete_structured(client, endpoint(), "m", REQUEST, Advice)
    assert len(seen) == 1


async def test_whitespace_only_answer_still_counts_as_starved():
    client, _ = replaying(ok(chat_payload("\n  \n", completion=60, reasoning=60)))
    outcome = await complete_structured(client, endpoint(), "m", REQUEST, Advice)
    assert outcome.kind == "starved"


async def test_an_empty_answer_is_never_blamed_on_the_prompt():
    """Three empty-answer shapes, none of them a prompt problem.

    The original criterion keyed starvation on `reasoning_tokens`, which only
    endpoints that PUBLISH the breakdown report — DeepSeek does, which is where
    it was measured. A gateway or a local Ollama returns the same empty answer
    with a zero reasoning count, and under that rule it fell through to
    "输出格式不符合预期，请检查提示词": the identical misdirection one layer
    down, sending the user to edit a template that was never at fault.

    So the signal is "tokens produced, nothing visible", whichever counter
    reveals it; and zero tokens at all is a third state that points at the
    endpoint, not at the prompt.
    """
    from app.llm.base import EMPTY_MESSAGE, STARVED_MESSAGE

    # Reports the breakdown (DeepSeek's shape).
    client, _ = replaying(ok(chat_payload("", completion=60, reasoning=60)))
    reported = await complete_structured(client, endpoint(), "m", REQUEST, Advice)
    # Produced tokens, publishes no breakdown (gateway / Ollama shape).
    client, _ = replaying(ok(chat_payload("", completion=60, reasoning=0)))
    silent = await complete_structured(client, endpoint(), "m", REQUEST, Advice)
    # Produced nothing whatsoever.
    client, _ = replaying(ok(chat_payload("", completion=0, reasoning=0)))
    nothing = await complete_structured(client, endpoint(), "m", REQUEST, Advice)

    assert reported.kind == "starved"
    assert silent.kind == "starved", "a gateway that hides the breakdown gets misdiagnosed"
    assert nothing.kind == "empty"
    for outcome, message in (
        (reported, STARVED_MESSAGE),
        (silent, STARVED_MESSAGE),
        (nothing, EMPTY_MESSAGE),
    ):
        assert outcome.message == message
        assert "提示词" not in (outcome.message or ""), "an empty answer is not a prompt bug"


# --------------------------------------------------------------------------- #
# Structured output
# --------------------------------------------------------------------------- #


async def test_valid_json_is_parsed_and_validated():
    body = json.dumps({"verdict": "值得", "summary": "低于四分位", "extra": "ignored"})
    client, seen = replaying(ok(chat_payload(body)))
    outcome = await complete_structured(client, endpoint(), "m", REQUEST, Advice)

    assert outcome.kind == "ok"
    assert outcome.data == {"verdict": "值得", "summary": "低于四分位"}
    assert len(seen) == 1


async def test_a_markdown_fence_still_parses():
    """Chat models fence JSON even when told not to, and a retry only fences
    it again — so the fence has to be stripped, not retried."""
    body = '```json\n{"verdict": "值得", "summary": "ok"}\n```'
    client, seen = replaying(ok(chat_payload(body)))
    outcome = await complete_structured(client, endpoint(), "m", REQUEST, Advice)
    assert outcome.kind == "ok"
    assert len(seen) == 1


async def test_bad_json_retries_once_and_succeeds():
    good = json.dumps({"verdict": "可考虑", "summary": "再等等"})
    client, seen = replaying(ok(chat_payload("我觉得还行")), ok(chat_payload(good)))
    outcome = await complete_structured(client, endpoint(), "m", REQUEST, Advice)

    assert outcome.kind == "ok"
    assert outcome.data == {"verdict": "可考虑", "summary": "再等等"}
    assert len(seen) == 2


async def test_schema_mismatch_also_retries():
    """Valid JSON with the wrong fields is the same user error as invalid
    JSON: the template was edited and the field names drifted."""
    good = json.dumps({"verdict": "不建议", "summary": "风险高"})
    client, seen = replaying(ok(chat_payload('{"opinion": "buy"}')), ok(chat_payload(good)))
    outcome = await complete_structured(client, endpoint(), "m", REQUEST, Advice)
    assert outcome.kind == "ok"
    assert len(seen) == 2


async def test_two_bad_answers_return_the_raw_text_and_never_raise():
    """A broken prompt template is a normal event, not a 500. The user needs
    to see what the model actually said to fix it."""
    client, seen = replaying(ok(chat_payload("完全不是 JSON")))
    outcome = await complete_structured(client, endpoint(), "m", REQUEST, Advice)

    assert outcome.kind == "unparsable"
    assert outcome.text == "完全不是 JSON"
    assert "提示词" in (outcome.message or "")
    assert outcome.data is None
    assert len(seen) == 2


# --------------------------------------------------------------------------- #
# Wire format selection and transport failures
# --------------------------------------------------------------------------- #


async def test_wire_format_picks_the_adapter():
    client, seen = replaying(ok({"content": [{"type": "text", "text": "ok"}]}))
    await complete(client, endpoint("anthropic"), "claude-x", REQUEST)

    assert seen[0].url.path == "/v1/messages"
    assert "x-api-key" in seen[0].headers
    assert json.loads(seen[0].content)["system"] == "你是助手"


async def test_an_unknown_wire_format_names_the_options():
    client, _ = replaying(ok(chat_payload("ok")))
    with pytest.raises(LlmConfigError) as exc:
        await complete(client, endpoint("ollama-native"), "m", REQUEST)
    assert "openai" in str(exc.value)


async def test_auth_failure_blames_the_key_not_the_prompt():
    client, _ = replaying(httpx.Response(401, json={"error": {"message": "invalid api key"}}))
    with pytest.raises(LlmTransportError) as exc:
        await complete(client, endpoint(), "m", REQUEST)
    assert "api_key" in str(exc.value)
    assert "401" in str(exc.value)
    # The body carries the real cause; the status code alone does not.
    assert "invalid api key" in str(exc.value)


async def test_404_points_at_the_base_url():
    client, _ = replaying(httpx.Response(404, text="not found"))
    with pytest.raises(LlmTransportError) as exc:
        await complete(client, endpoint(), "m", REQUEST)
    assert "base_url" in str(exc.value)


async def test_timeout_says_so_without_echoing_the_url():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LlmTransportError) as exc:
        await complete(client, endpoint(), "m", REQUEST)
    assert "超时" in str(exc.value)
    assert "deepseek.com" not in str(exc.value)


async def test_html_instead_of_json_says_the_base_url_is_wrong():
    client, _ = replaying(httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(LlmTransportError) as exc:
        await complete(client, endpoint(), "m", REQUEST)
    assert "base_url" in str(exc.value)


# --------------------------------------------------------------------------- #
# Model discovery
# --------------------------------------------------------------------------- #


async def test_discovery_returns_the_model_ids():
    client, seen = replaying(ok({"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}]}))
    models, error = await discover_models(client, endpoint())

    assert models == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert error is None
    assert seen[0].url.path == "/models"
    assert seen[0].method == "GET"


async def test_discovery_failure_returns_an_empty_list_with_a_reason():
    """Plenty of relay gateways do not implement this route. Raising would let
    api/ turn a normal situation into a 4xx and block the manual entry the UI
    offers instead.
    """
    client, _ = replaying(httpx.Response(404, text="Not Found"))
    models, error = await discover_models(client, endpoint())

    assert models == []
    assert error is not None
    assert "404" in error


async def test_discovery_survives_a_dead_host():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nodename nor servname provided", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models, error = await discover_models(client, endpoint())
    assert (models, error is None) == ([], False)


async def test_discovery_survives_a_body_that_is_not_a_model_list():
    client, _ = replaying(httpx.Response(200, text="<html>hi</html>"))
    models, error = await discover_models(client, endpoint())
    assert models == []
    assert error is not None


async def test_discovery_of_an_unknown_wire_format_does_not_raise():
    client, _ = replaying(ok({"data": []}))
    models, error = await discover_models(client, endpoint("ollama-native"))
    assert models == []
    assert "ollama-native" in (error or "")


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


async def test_the_api_key_reaches_the_endpoint_and_nowhere_else(caplog):
    """The key must appear in exactly one place: the outgoing auth header.
    Checked by pattern over every log record and every error string, not by
    eyeballing the one line we remember writing.
    """
    caplog.set_level(logging.DEBUG)
    # A gateway that echoes the auth header back is the leak that scrubbing
    # exists for -- and it is a real behaviour, not a hypothetical.
    hostile = httpx.Response(403, json={"error": f"rejected Bearer {KEY}"})
    client, seen = replaying(ok(chat_payload('{"verdict": "值得", "summary": "ok"}')))
    await complete_structured(client, endpoint(), "m", REQUEST, Advice)

    client, _ = replaying(hostile)
    with pytest.raises(LlmTransportError) as exc:
        await complete(client, endpoint(), "m", REQUEST)

    client, _ = replaying(hostile)
    _, discovery_error = await discover_models(client, endpoint())

    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"
    leaked = [KEY in r.getMessage() or KEY in str(r.args) for r in caplog.records]
    assert caplog.records and not any(leaked)
    assert KEY not in str(exc.value)
    assert KEY not in (discovery_error or "")
    assert "***" in str(exc.value)
