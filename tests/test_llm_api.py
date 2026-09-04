"""LLM endpoint and scenario configuration.

Never touches the network: every LLM call goes through `httpx.MockTransport`
on `app.state.notify_client`, the same way `tests/test_llm_client.py` does. A
test that reaches a real endpoint is a flake with a bill attached.

The load-bearing test in this file is `test_the_api_key_never_appears_in_any
_response`: it sweeps every route of the router and scans by PATTERN, not for
the one value it happens to know. `docs/m1-report.md` records why — grepping
for a known secret passes while the next-shaped one leaks.
"""

import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session

from app.db import get_session
from app.llm.prompts import PLACEHOLDERS
from app.main import app
from app.models import LlmEndpoint, LlmScenarioConfig

AUTH = {"Authorization": "Bearer testtoken123"}

KEY = "sk-liveKey0123456789abcdef"
ENDPOINT = {
    "label": "deepseek",
    "base_url": "https://api.deepseek.com",
    "api_key": KEY,
    "wire_format": "openai",
}

# What a leaked credential looks like without knowing which one it is: the
# prefix every OpenAI-compatible gateway uses, plus a long opaque run. This is
# the "audit by pattern" half; the literal-value assertion is the other half.
SECRET_SHAPED = re.compile(r"sk-[A-Za-z0-9_-]{8,}")

MODELS_PAYLOAD = {
    "data": [
        {"id": "deepseek-v4-flash"},
        {"id": "deepseek-v4-pro"},
        {"id": "deepseek-v4-flash-vision-exp"},
    ]
}


def chat_payload(content: str, completion: int = 13, reasoning: int = 11) -> dict:
    """Shaped like the measured response, reasoning breakdown included."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 24,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def stub(handler) -> None:
    """Point the app's shared client at a fake transport."""
    app.state.notify_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def gateway(
    *, models: httpx.Response | None = None, chat: httpx.Response | None = None
) -> list[httpx.Request]:
    """A stand-in gateway answering both routes. Returns the request log."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return models or httpx.Response(200, json=MODELS_PAYLOAD)
        return chat or httpx.Response(200, json=chat_payload("ok"))

    stub(handler)
    return seen


@pytest.fixture
def client():
    from tests.conftest import memory_engine

    engine = memory_engine()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    gateway()
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def create(c: TestClient, **overrides) -> int:
    r = c.post("/api/llm/endpoints", json={**ENDPOINT, **overrides}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --------------------------------------------------------------------------- #
# The key
# --------------------------------------------------------------------------- #


def test_the_api_key_never_appears_in_any_response(client):
    """Every route of this router, swept in one test.

    A per-route assertion would pass for the routes someone remembered. The
    sweep is what covers the next route added below it.
    """
    c, _ = client
    endpoint_id = create(c)
    # A failing gateway too: the error path is where a key usually escapes,
    # because a gateway can echo the Authorization header back in its body.
    gateway(
        models=httpx.Response(401, json={"error": f"invalid key: {KEY}"}),
        chat=httpx.Response(401, json={"error": f"invalid key: {KEY}"}),
    )
    c.put(
        "/api/llm/scenarios/market",
        json={"endpoint_id": endpoint_id, "model": "deepseek-v4-pro", "enabled": True},
        headers=AUTH,
    )

    responses = [
        c.get("/api/llm/endpoints", headers=AUTH),
        c.post("/api/llm/endpoints", json={**ENDPOINT, "label": "second"}, headers=AUTH),
        c.patch(f"/api/llm/endpoints/{endpoint_id}", json={"api_key": KEY}, headers=AUTH),
        c.get(f"/api/llm/endpoints/{endpoint_id}/models", headers=AUTH),
        c.post(f"/api/llm/endpoints/{endpoint_id}/test?model=deepseek-v4-pro", headers=AUTH),
        c.get("/api/llm/scenarios/market", headers=AUTH),
        c.put("/api/llm/scenarios/item", json={"endpoint_id": endpoint_id}, headers=AUTH),
        c.get("/api/llm/scenarios/market/default-prompt", headers=AUTH),
        c.get("/openapi.json"),
    ]

    for r in responses:
        assert KEY not in r.text, f"{r.request.method} {r.request.url.path} echoed the key"
        leaked = SECRET_SHAPED.search(r.text)
        assert leaked is None, f"{r.request.url.path} carries a secret-shaped value: {leaked}"


def test_no_response_model_even_has_a_key_field(client):
    """Belt to the sweep's braces: the presence flag is a bool and there is no
    `api_key` field to forget to redact."""
    c, _ = client
    body = c.get(f"/api/llm/endpoints/{create(c)}/models", headers=AUTH)
    assert body.status_code == 200
    listed = c.get("/api/llm/endpoints", headers=AUTH).json()[0]
    assert listed["api_key_configured"] is True
    assert "api_key" not in listed


def test_the_real_key_is_stored_and_sent_to_the_endpoint(client):
    """The flip side: a key that is never echoed still has to be usable."""
    c, engine = client
    endpoint_id = create(c)
    with Session(engine) as s:
        assert s.get(LlmEndpoint, endpoint_id).api_key == KEY

    seen = gateway()
    c.get(f"/api/llm/endpoints/{endpoint_id}/models", headers=AUTH)
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


def test_an_omitted_key_survives_a_patch(client):
    """The page submits the form without a key whenever the user did not
    retype it. Taking that as "clear it" would silently break the endpoint.
    """
    c, engine = client
    endpoint_id = create(c)
    r = c.patch(f"/api/llm/endpoints/{endpoint_id}", json={"label": "renamed"}, headers=AUTH)
    assert r.json()["label"] == "renamed"
    assert r.json()["api_key_configured"] is True
    with Session(engine) as s:
        assert s.get(LlmEndpoint, endpoint_id).api_key == KEY


def test_an_empty_key_clears_it(client):
    """A local Ollama needs no key, so clearing has to be expressible."""
    c, engine = client
    endpoint_id = create(c)
    r = c.patch(f"/api/llm/endpoints/{endpoint_id}", json={"api_key": ""}, headers=AUTH)
    assert r.json()["api_key_configured"] is False
    with Session(engine) as s:
        assert s.get(LlmEndpoint, endpoint_id).api_key == ""


def test_the_key_stays_out_of_the_logs(client, caplog):
    """`logging-guidelines.md`: secrets are redacted at the point of logging,
    not by a filter someone has to remember to install."""
    c, _ = client
    with caplog.at_level("DEBUG"):
        endpoint_id = create(c)
        c.get(f"/api/llm/endpoints/{endpoint_id}/models", headers=AUTH)
        c.post(f"/api/llm/endpoints/{endpoint_id}/test?model=m", headers=AUTH)
    assert KEY not in caplog.text
    assert SECRET_SHAPED.search(caplog.text) is None


# --------------------------------------------------------------------------- #
# Endpoint CRUD
# --------------------------------------------------------------------------- #


def test_crud_round_trip(client):
    c, _ = client
    endpoint_id = create(c)
    assert len(c.get("/api/llm/endpoints", headers=AUTH).json()) == 1
    patched = c.patch(
        f"/api/llm/endpoints/{endpoint_id}", json={"wire_format": "anthropic"}, headers=AUTH
    )
    assert patched.json()["wire_format"] == "anthropic"
    assert c.delete(f"/api/llm/endpoints/{endpoint_id}", headers=AUTH).status_code == 204
    assert c.get("/api/llm/endpoints", headers=AUTH).json() == []


def test_unknown_wire_format_is_422_not_500(client):
    """Literal, not str: an unknown format would otherwise reach
    `client.adapter_for` and surface as a 500 on the first call instead of a
    rejected form field."""
    c, _ = client
    r = c.post("/api/llm/endpoints", json={**ENDPOINT, "wire_format": "ollama"}, headers=AUTH)
    assert r.status_code == 422


def test_missing_endpoint_is_404(client):
    c, _ = client
    assert c.patch("/api/llm/endpoints/999", json={"label": "x"}, headers=AUTH).status_code == 404
    assert c.delete("/api/llm/endpoints/999", headers=AUTH).status_code == 404
    assert c.get("/api/llm/endpoints/999/models", headers=AUTH).status_code == 404
    assert c.post("/api/llm/endpoints/999/test?model=m", headers=AUTH).status_code == 404


def test_deleting_an_endpoint_detaches_its_scenarios(client):
    """A dangling endpoint_id would fail inside the analyze path, one layer
    away from the cause."""
    c, engine = client
    endpoint_id = create(c)
    c.put(
        "/api/llm/scenarios/item",
        json={"endpoint_id": endpoint_id, "model": "m", "enabled": True},
        headers=AUTH,
    )
    c.delete(f"/api/llm/endpoints/{endpoint_id}", headers=AUTH)

    with Session(engine) as s:
        row = s.get(LlmScenarioConfig, "item")
    assert (row.endpoint_id, row.enabled) == (None, False)


def test_query_count_does_not_grow_with_rows(client):
    """EQUALITY, not a bound: a per-row query passes `< 10` at 3 rows and
    regresses quietly at 30."""
    c, engine = client

    def seed(count: int) -> None:
        with Session(engine) as s:
            for i in range(count):
                s.add(
                    LlmEndpoint(
                        label=f"e{i}", base_url="https://x", api_key=KEY, wire_format="openai"
                    )
                )
            s.commit()

    statements: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    seed(3)
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert len(c.get("/api/llm/endpoints", headers=AUTH).json()) == 3
        few = len(statements)
        event.remove(engine, "before_cursor_execute", record)
        seed(27)
        statements.clear()
        event.listen(engine, "before_cursor_execute", record)
        assert len(c.get("/api/llm/endpoints", headers=AUTH).json()) == 30
        many = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert few == many, f"{few} queries for 3 rows vs {many} for 30"


# --------------------------------------------------------------------------- #
# Model discovery
# --------------------------------------------------------------------------- #


def test_discovery_lists_the_models(client):
    c, _ = client
    endpoint_id = create(c)
    r = c.get(f"/api/llm/endpoints/{endpoint_id}/models", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "models": ["deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro"],
        "error": None,
    }


def test_discovery_failure_is_200_with_a_reason(client):
    """Many gateways never implement this route. A 4xx would read as "your
    endpoint is broken" and block a user who only needs to type the name.
    """
    c, _ = client
    endpoint_id = create(c)
    gateway(models=httpx.Response(404, text="Not Found"))
    r = c.get(f"/api/llm/endpoints/{endpoint_id}/models", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["models"] == []
    assert "404" in r.json()["error"]


def test_discovery_survives_a_dead_host(client):
    c, _ = client
    endpoint_id = create(c)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nodename nor servname provided", request=request)

    stub(handler)
    r = c.get(f"/api/llm/endpoints/{endpoint_id}/models", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["models"] == []
    assert r.json()["error"]


# --------------------------------------------------------------------------- #
# Test connection
# --------------------------------------------------------------------------- #


def test_test_connection_uses_the_production_call_path(client):
    c, _ = client
    endpoint_id = create(c)
    seen = gateway(chat=httpx.Response(200, json=chat_payload("ok")))
    r = c.post(f"/api/llm/endpoints/{endpoint_id}/test?model=deepseek-v4-flash", headers=AUTH)

    assert r.json() == {"ok": True, "error": None, "text": "ok"}
    chat = seen[-1]
    assert chat.url.path == "/chat/completions"
    assert chat.method == "POST"


def test_test_connection_reports_a_refusal_without_raising(client):
    """The whole point of the button is diagnosing exactly this, so it must
    not be a 500."""
    c, _ = client
    endpoint_id = create(c)
    gateway(chat=httpx.Response(401, text="Unauthorized"))
    r = c.post(f"/api/llm/endpoints/{endpoint_id}/test?model=m", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "api_key" in r.json()["error"]


def test_an_empty_answer_blames_the_token_budget_not_the_prompt(client):
    """Measured: max_tokens=60 gave 60 completion tokens, all reasoning, and
    an empty string with finish_reason "stop". "Check your prompt" here sends
    the user to debug the one thing that is not wrong — and there is no prompt
    involved in a connection test at all.
    """
    c, _ = client
    endpoint_id = create(c)
    gateway(chat=httpx.Response(200, json=chat_payload("", completion=60, reasoning=60)))
    body = c.post(f"/api/llm/endpoints/{endpoint_id}/test?model=m", headers=AUTH).json()

    assert body["ok"] is False
    assert "max_tokens" in body["error"]
    assert "提示词" not in body["error"]


def test_nothing_at_all_points_at_the_endpoint_not_the_budget(client):
    c, _ = client
    endpoint_id = create(c)
    gateway(chat=httpx.Response(200, json=chat_payload("", completion=0, reasoning=0)))
    body = c.post(f"/api/llm/endpoints/{endpoint_id}/test?model=m", headers=AUTH).json()

    assert body["ok"] is False
    assert "max_tokens" not in body["error"]
    assert "模型 id" in body["error"]


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def test_an_unsaved_scenario_is_a_shape_not_a_404(client):
    """Every install starts here, and the page needs something to render."""
    c, _ = client
    r = c.get("/api/llm/scenarios/market", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "scenario": "market",
        "endpoint_id": None,
        "model": None,
        "prompt_template": None,
        "send_images": False,
        "max_tokens": None,
        "enabled": False,
    }


def test_scenario_upsert_round_trip(client):
    c, _ = client
    endpoint_id = create(c)
    saved = c.put(
        "/api/llm/scenarios/item",
        json={
            "endpoint_id": endpoint_id,
            "model": "deepseek-v4-flash-vision-exp",
            "prompt_template": "看看 {item}",
            "send_images": True,
            "enabled": True,
        },
        headers=AUTH,
    )
    assert saved.status_code == 200
    assert saved.json()["send_images"] is True
    # A second PUT updates the same row rather than failing on the primary key
    again = c.put("/api/llm/scenarios/item", json={"model": "other"}, headers=AUTH)
    assert again.json() == {
        "scenario": "item",
        "endpoint_id": None,
        "model": "other",
        "prompt_template": None,
        "send_images": False,
        "max_tokens": None,
        "enabled": False,
    }


def test_market_cannot_store_a_send_images_it_will_never_honour(client):
    """The market path never attaches an image, so accepting `true` there
    would record a setting the code cannot act on. A config row that lies is
    worse than one missing a field -- someone reading it later believes the
    photos went out.
    """
    c, _ = client
    saved = c.put("/api/llm/scenarios/market", json={"send_images": True}, headers=AUTH)
    assert saved.status_code == 200
    assert saved.json()["send_images"] is False

    # The item path, where it means something, keeps it.
    item = c.put("/api/llm/scenarios/item", json={"send_images": True}, headers=AUTH)
    assert item.json()["send_images"] is True


def test_the_token_budget_is_configurable_and_bounded(client):
    """`starved` tells the user to raise max_tokens. Until this was on the
    scenario row the only way to take that advice was editing
    `app/llm/base.py`, so the message named a knob the product did not offer.

    Bounded because 4096 is the value measured to starve the market prompt:
    the floor stops someone configuring the failure they were just told to fix.
    """
    c, _ = client
    saved = c.put("/api/llm/scenarios/market", json={"max_tokens": 32_768}, headers=AUTH)
    assert saved.status_code == 200
    assert saved.json()["max_tokens"] == 32_768

    assert (
        c.put("/api/llm/scenarios/market", json={"max_tokens": 512}, headers=AUTH).status_code
        == 422
    )
    assert (
        c.put("/api/llm/scenarios/market", json={"max_tokens": 200_000}, headers=AUTH).status_code
        == 422
    )
    # Omitted means "the built-in default", not zero.
    cleared = c.put("/api/llm/scenarios/market", json={}, headers=AUTH)
    assert cleared.json()["max_tokens"] is None


def test_unknown_scenario_is_422_not_500(client):
    """`scenario` is a Literal: a bad value is a rejected request, and the two
    valid ones reach OpenAPI as an enum so the frontend types are a union."""
    c, _ = client
    assert c.get("/api/llm/scenarios/weather", headers=AUTH).status_code == 422
    assert c.put("/api/llm/scenarios/weather", json={}, headers=AUTH).status_code == 422
    assert c.get("/api/llm/scenarios/weather/default-prompt", headers=AUTH).status_code == 422


def test_a_scenario_cannot_point_at_a_missing_endpoint(client):
    c, _ = client
    r = c.put("/api/llm/scenarios/market", json={"endpoint_id": 999}, headers=AUTH)
    assert r.status_code == 404


def test_a_broken_template_is_saved_verbatim_and_does_not_500(client):
    """Users edit these, so a typo'd placeholder is a normal event.
    `prompts.render` substitutes with str.replace, so an unknown placeholder
    survives into the prompt as visible text by design — rejecting it here
    would be a 422 on a field someone is mid-edit in.
    """
    c, _ = client
    broken = "水位是 {stats} 吗？{plcaeholder} {{unbalanced"
    r = c.put("/api/llm/scenarios/market", json={"prompt_template": broken}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["prompt_template"] == broken
    assert c.get("/api/llm/scenarios/market", headers=AUTH).json()["prompt_template"] == broken


def test_clearing_the_template_restores_the_default(client):
    """Null and "" both mean "use the built-in one": storing an empty template
    would send the model a prompt with no instructions in it."""
    c, _ = client
    c.put("/api/llm/scenarios/market", json={"prompt_template": "custom"}, headers=AUTH)
    r = c.put("/api/llm/scenarios/market", json={"prompt_template": ""}, headers=AUTH)
    assert r.json()["prompt_template"] is None


def test_default_prompt_publishes_the_placeholder_contract(client):
    """The contract has to be listed for a user to edit a template at all, and
    it must match the template we ship — a placeholder in the list that the
    default does not use is a promise nothing keeps."""
    c, _ = client
    for scenario in ("market", "item"):
        body = c.get(f"/api/llm/scenarios/{scenario}/default-prompt", headers=AUTH).json()
        assert body["scenario"] == scenario
        assert body["placeholders"] == list(PLACEHOLDERS[scenario])
        for name in body["placeholders"]:
            assert "{" + name + "}" in body["prompt_template"], name


def test_auth_required(client):
    c, _ = client
    assert c.get("/api/llm/endpoints").status_code == 401
    assert c.post("/api/llm/endpoints", json=ENDPOINT).status_code == 401
    assert c.get("/api/llm/endpoints/1/models").status_code == 401
    assert c.post("/api/llm/endpoints/1/test?model=m").status_code == 401
    assert c.get("/api/llm/scenarios/market").status_code == 401
    assert c.put("/api/llm/scenarios/market", json={}).status_code == 401
    assert c.get("/api/llm/scenarios/market/default-prompt").status_code == 401
