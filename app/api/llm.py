"""LLM endpoint and scenario configuration.

Two tables that existed since M1 with nothing ever reading or writing them
(`docs/m1-report.md`: "做一个存了也无从验证的表单比没有更糟"). This is the
surface that makes them real, and it is only config — the two analyze routes
land here later, in T3/T4.

`api_key` is write-only, in the strongest sense available: no response model
in this module has a field for it, so there is no return statement that could
leak one by forgetting to redact. The page learns `api_key_configured` and
nothing more.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel import Session, select

from app.db import SessionDep
from app.llm import client as llm_client
from app.llm import prompts
from app.llm.base import EMPTY_MESSAGE, STARVED_MESSAGE, LlmError, LlmRequest
from app.models import LlmEndpoint, LlmScenarioConfig
from app.schemas import (
    LlmDefaultPrompt,
    LlmEndpointCreate,
    LlmEndpointPublic,
    LlmEndpointUpdate,
    LlmModelList,
    LlmScenarioPublic,
    LlmScenarioUpdate,
    LlmTestResult,
    Scenario,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/llm", tags=["llm"])

ModelName = Annotated[str, Query(min_length=1, max_length=200)]

# A real completion through the production code path, the same way
# `api/channels.py` tests a channel with the scheduler's own `deliver()`: a
# cheaper probe would pass while real calls fail.
#
# Keeps LlmRequest's default max_tokens deliberately. Measured on this
# endpoint, even "回复 ok" spent 11 reasoning tokens, so a budget sized to the
# expected answer buys an empty string rather than a shorter one.
TEST_REQUEST = LlmRequest(
    system="你是连通性自检助手，只按要求回复，不要解释。",
    user_text="回复 ok",
)


def _public(endpoint: LlmEndpoint) -> LlmEndpointPublic:
    assert endpoint.id is not None
    return LlmEndpointPublic(
        id=endpoint.id,
        label=endpoint.label,
        base_url=endpoint.base_url,
        wire_format=endpoint.wire_format,
        api_key_configured=bool(endpoint.api_key),
        created_at=endpoint.created_at,
    )


def _scenario_public(row: LlmScenarioConfig) -> LlmScenarioPublic:
    return LlmScenarioPublic(
        scenario=row.scenario,
        endpoint_id=row.endpoint_id,
        model=row.model,
        prompt_template=row.prompt_template,
        send_images=row.send_images,
        enabled=row.enabled,
    )


def _require(session: Session, endpoint_id: int) -> LlmEndpoint:
    endpoint = session.get(LlmEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="llm endpoint not found")
    return endpoint


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("/endpoints", response_model=list[LlmEndpointPublic])
def list_endpoints(session: SessionDep) -> list[LlmEndpointPublic]:
    """One query regardless of how many endpoints exist.

    `api_key_configured` is derived from the row that is already loaded, so
    there is nothing here to turn into a per-row lookup — pinned by a test
    that compares query counts at 3 and 30 rows for equality.

    ponytail: no `limit`, like `/api/channels`. These rows are typed in by
    hand, one per gateway; add paging when someone has 200 of them.
    """
    rows = session.exec(select(LlmEndpoint).order_by(LlmEndpoint.id)).all()  # type: ignore[arg-type]
    return [_public(row) for row in rows]


@router.post("/endpoints", response_model=LlmEndpointPublic, status_code=201)
def create_endpoint(payload: LlmEndpointCreate, session: SessionDep) -> LlmEndpointPublic:
    endpoint = LlmEndpoint(
        label=payload.label,
        base_url=payload.base_url,
        api_key=payload.api_key,
        wire_format=payload.wire_format,
    )
    session.add(endpoint)
    session.commit()
    session.refresh(endpoint)
    # Never the key, and never the base_url either: a user-pasted base_url can
    # carry credentials in a query parameter (`llm/client.py` drops httpx's
    # exception text for the same reason).
    log.info(
        "llm endpoint created",
        extra={
            "endpoint_id": endpoint.id,
            "wire_format": endpoint.wire_format,
            "has_key": bool(endpoint.api_key),
        },
    )
    return _public(endpoint)


@router.patch("/endpoints/{endpoint_id}", response_model=LlmEndpointPublic)
def update_endpoint(
    endpoint_id: int, payload: LlmEndpointUpdate, session: SessionDep
) -> LlmEndpointPublic:
    """An omitted `api_key` keeps the stored one; `""` clears it.

    No "was this the redacted placeholder" check is needed, unlike
    `channels.py`: nothing ever handed the page a value to send back.
    """
    endpoint = _require(session, endpoint_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "api_key" and value is None:
            continue
        setattr(endpoint, key, value)
    session.commit()
    session.refresh(endpoint)
    return _public(endpoint)


@router.delete("/endpoints/{endpoint_id}", status_code=204)
def delete_endpoint(endpoint_id: int, session: SessionDep) -> None:
    """Detaches the scenarios that pointed here, and disables them.

    Leaving `endpoint_id` dangling would make the analyze routes fail with
    whatever a missing row turns into, one layer away from the cause. A
    scenario with no endpoint cannot run at all, so it goes back to disabled
    rather than staying green in the UI.
    """
    endpoint = _require(session, endpoint_id)
    orphaned = session.exec(
        select(LlmScenarioConfig).where(LlmScenarioConfig.endpoint_id == endpoint_id)
    ).all()
    for row in orphaned:
        row.endpoint_id = None
        row.enabled = False
    session.delete(endpoint)
    session.commit()
    log.info(
        "llm endpoint deleted",
        extra={"endpoint_id": endpoint_id, "scenarios_detached": len(orphaned)},
    )


@router.get("/endpoints/{endpoint_id}/models", response_model=LlmModelList)
async def list_endpoint_models(
    endpoint_id: int, session: SessionDep, request: Request
) -> LlmModelList:
    """Available models, or an empty list and a reason — always HTTP 200.

    A deliberate exception to `error-handling.md`'s "errors use status codes":
    a gateway that does not implement the models route is not an error, it is
    the common case (the probed DeepSeek endpoint does implement it, which is
    why the happy path is testable at all). The user's next action either way
    is to type the model name, and a 4xx here would read as "your endpoint is
    broken" and stop them. `discover_models` never raises for that reason.

    The 404 below is a different thing: that is OUR row missing, not theirs.
    """
    endpoint = _require(session, endpoint_id)
    models, error = await llm_client.discover_models(request.app.state.notify_client, endpoint)
    log.info(
        "llm model discovery",
        extra={"endpoint_id": endpoint_id, "count": len(models), "failed": error is not None},
    )
    return LlmModelList(models=models, error=error)


@router.post("/endpoints/{endpoint_id}/test", response_model=LlmTestResult)
async def test_endpoint(
    endpoint_id: int, model: ModelName, session: SessionDep, request: Request
) -> LlmTestResult:
    """One real completion, so "test connection" tests the connection.

    Three distinct not-ok outcomes, because they have three different fixes:
    the endpoint refused us (check base_url / key / model id), the model spent
    the whole budget thinking (raise max_tokens), or nothing came back at all.
    Collapsing them is the misdirected error `error-handling.md` forbids.
    """
    endpoint = _require(session, endpoint_id)
    try:
        response = await llm_client.complete(
            request.app.state.notify_client, endpoint, model, TEST_REQUEST
        )
    except LlmError as exc:
        # str() of OUR error family only. Its messages are secret-free by
        # construction (`llm/base.py`); an httpx exception's str() carries the
        # request URL, which can hold the key.
        return LlmTestResult(ok=False, error=str(exc))
    if response.starved:
        return LlmTestResult(ok=False, error=STARVED_MESSAGE)
    if response.empty:
        return LlmTestResult(ok=False, error=EMPTY_MESSAGE)
    return LlmTestResult(ok=True, text=response.text)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


@router.get("/scenarios/{scenario}", response_model=LlmScenarioPublic)
def read_scenario(scenario: Scenario, session: SessionDep) -> LlmScenarioPublic:
    """No stored row is the state every install starts in, not a 404.

    The config page needs a shape to render before anything has been saved,
    and the defaults it should render are exactly the model's own.
    """
    row = session.get(LlmScenarioConfig, scenario)
    return _scenario_public(row or LlmScenarioConfig(scenario=scenario))


@router.put("/scenarios/{scenario}", response_model=LlmScenarioPublic)
def save_scenario(
    scenario: Scenario, payload: LlmScenarioUpdate, session: SessionDep
) -> LlmScenarioPublic:
    """Upsert. PUT rather than PATCH: there is one row per scenario and the
    page owns the whole form, so a partial update has no meaning here.

    The template is stored as given. Placeholders are not validated — see
    `LlmScenarioUpdate` for why an unknown one is left alone on purpose.
    """
    if payload.endpoint_id is not None:
        _require(session, payload.endpoint_id)
    row = session.get(LlmScenarioConfig, scenario) or LlmScenarioConfig(scenario=scenario)
    row.endpoint_id = payload.endpoint_id
    row.model = payload.model or None
    # "" and None both mean "use the built-in default": a user who clears the
    # box is asking for the default back, and storing an empty template would
    # send the model a prompt with no instructions in it.
    row.prompt_template = payload.prompt_template or None
    row.send_images = payload.send_images
    row.enabled = payload.enabled
    session.add(row)
    session.commit()
    session.refresh(row)
    log.info(
        "llm scenario saved",
        extra={
            "scenario": scenario,
            "endpoint_id": row.endpoint_id,
            "enabled": row.enabled,
            "send_images": row.send_images,
            "custom_prompt": row.prompt_template is not None,
        },
    )
    return _scenario_public(row)


@router.get("/scenarios/{scenario}/default-prompt", response_model=LlmDefaultPrompt)
def read_default_prompt(scenario: Scenario) -> LlmDefaultPrompt:
    """The built-in template plus the placeholder contract.

    Both in one response because the page needs both at once: "restore
    default" writes the template, and the list beside the editor is the only
    place a user can learn what `{stats}` is allowed to be.
    """
    return LlmDefaultPrompt(
        scenario=scenario,
        prompt_template=prompts.default_prompt(scenario),
        placeholders=list(prompts.PLACEHOLDERS[scenario]),
    )
