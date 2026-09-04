"""Boundary types and errors for the LLM layer.

`LlmRequest` and `LlmResponse` are the only shapes allowed to cross into or
out of an adapter. Vendor payloads stay inside `openai.py` / `anthropic.py`
(apart from `LlmResponse.raw`, kept for debugging) so no caller grows its own
guesses about where the text lives.

`llm/` has no HTTP context, so it never raises `HTTPException` — see
`spec/backend/error-handling.md`. It raises the `LlmError` family, whose
messages are user-facing and therefore secret-free.
"""

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

# Both wire formats need the mime type, and neither can be told it: LlmRequest
# carries bare base64 (design §2). OpenAI puts it in the data: URL, Anthropic
# in source.media_type.
# Truncated at a base64 group boundary (4 chars = 3 bytes): a longer prefix
# would depend on bytes that come after the magic and stop matching.
_MAGIC = {"iVBORw0K": "image/png", "R0lG": "image/gif", "UklG": "image/webp"}


class LlmError(Exception):
    """Base of the LLM error family. Its message is shown to the user.

    "Shown to the user" is load-bearing: an api_key travels in every request
    to these endpoints, so no member of this family may carry an exception's
    str(), a request URL, or an unscrubbed response body.
    """


class LlmConfigError(LlmError):
    """Stored endpoint or scenario config cannot be turned into a request."""


class LlmTransportError(LlmError):
    """The endpoint was unreachable, refused us, timed out, or answered with
    something that is not a chat completion at all."""


# The three failure modes of design §4 get three messages. Two of them arrive
# as HTTP 200 and are NOT interchangeable: an empty answer from a reasoning
# model is fixed by raising max_tokens, and telling that user to check the
# prompt is exactly the misdirected error `error-handling.md` forbids.
STARVED_MESSAGE = "模型把 token 预算全部用在推理上，没有留下可见回答。请把 max_tokens 调大后重试。"
# An empty answer with no tokens produced at all is a third thing, and it is
# still NOT a prompt problem -- the model never got as far as writing anything
# for the prompt to be wrong about. Saying "检查提示词" here sends the user to
# edit a template when the endpoint or the model id is what needs looking at.
EMPTY_MESSAGE = (
    "端点返回了空回答，而且没有产生任何 token。这通常是模型 id 写错、"
    "端点配置不对，或者请求被上游静默拒绝了；先用「测试连接」和「拉取模型列表」排查。"
)
UNPARSABLE_MESSAGE = "输出格式不符合预期，请检查提示词。下面是模型返回的原始内容。"


@dataclass(frozen=True, slots=True)
class LlmImage:
    """One image, carrying the mime type the downloader already knew.

    Not a bare base64 string: the fetcher reads `Content-Type` off the
    response, and throwing that away only to sniff it back from magic bytes in
    the adapter destroys information and then guesses it. Sniffing also fails
    silently on a format whose magic we do not list, and a wrong label is a
    request the endpoint rejects for a reason that points nowhere useful.
    """

    media_type: str
    b64: str


@dataclass(frozen=True, slots=True)
class LlmRequest:
    system: str
    user_text: str
    images: tuple[LlmImage, ...] = ()
    # Far above any answer we ask for, deliberately, and 4096 was NOT far
    # enough. Measured end to end against the real endpoint on the market
    # prompt (~1100 chars in, a small JSON object out):
    #
    #   max_tokens=4096   -> starved, empty answer, 37s
    #   max_tokens=16384  -> ok, a five-field JSON object, 59s
    #
    # These are reasoning models: even "reply ok" spent 11 of its 13
    # completion tokens thinking. A tight budget does not buy a shorter
    # answer, it buys an empty string with finish_reason "stop" and no error
    # anywhere -- which is why `starved` exists and why the default has to sit
    # well clear of it rather than at the smallest value that ever worked.
    max_tokens: int = 16384


@dataclass(frozen=True, slots=True)
class LlmResponse:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    # Why these two are separate fields rather than derived from `raw`:
    # a reasoning model can spend the entire completion budget thinking and
    # return an empty `text` with finish_reason "stop" and no error anywhere.
    # Measured on this endpoint: max_tokens=60 gave 60 reasoning tokens and an
    # empty string. Carrying the counts explicitly is what lets the caller say
    # "raise max_tokens" instead of "check your prompt".
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def starved(self) -> bool:
        """Empty answer even though the model produced tokens.

        Keyed on `completion_tokens`, not on `reasoning_tokens`. Keying on the
        reasoning count only catches endpoints that PUBLISH the breakdown:
        DeepSeek does, and that is where this was measured (max_tokens=60 gave
        60 reasoning tokens and an empty string). A gateway or a local Ollama
        that reports no breakdown returns the same empty answer with a zero
        reasoning count, falls through to "output format unexpected", and the
        user is told to check a prompt that was never the problem — the same
        misdirection one layer down.

        Tokens produced with nothing visible to show for them is the whole
        signal; which counter reveals it is the endpoint's business.
        """
        return not self.text.strip() and self.completion_tokens > 0

    @property
    def empty(self) -> bool:
        """Nothing at all came back — not even tokens."""
        return not self.text.strip() and self.completion_tokens <= 0


@dataclass(frozen=True, slots=True)
class LlmOutcome:
    """A structured-output call as the UI has to render it.

    `text` is always the model's own output, so the "unparsable" state can
    show it verbatim: a user breaking the prompt template is a normal event,
    not an exception.
    """

    kind: Literal["ok", "starved", "empty", "unparsable"]
    text: str
    data: dict[str, Any] | None = None
    message: str | None = None


class LlmAdapter(Protocol):
    """One wire format. Two implementations, which is what earns the protocol.

    Every method is pure: the client owns the HTTP call so that the injected
    httpx client stays the only way to reach the network.
    """

    wire_format: ClassVar[str]

    def chat_url(self, base_url: str) -> str: ...

    def models_url(self, base_url: str) -> str: ...

    def headers(self, api_key: str) -> dict[str, str]: ...

    def body(self, model: str, request: LlmRequest) -> dict[str, Any]: ...

    def text(self, payload: dict[str, Any]) -> str: ...

    def usage(self, payload: dict[str, Any]) -> tuple[int, int]:
        """(completion_tokens, reasoning_tokens)."""
        ...

    def models(self, payload: dict[str, Any]) -> list[str]: ...


def media_type(image_b64: str) -> str:
    """Sniff the image mime from its base64 prefix.

    The FALLBACK, for a caller that has no Content-Type to pass. Prefer
    building `LlmImage` from the response header: this function cannot see a
    format whose magic bytes are not listed below.

    Defaults to jpeg because the goofish CDN serves jpg for listing photos;
    guessing wrong on the label is survivable, sending no label is not.
    """
    for prefix, mime in _MAGIC.items():
        if image_b64.startswith(prefix):
            return mime
    return "image/jpeg"


def join_url(base_url: str, path: str) -> str:
    """Users paste base URLs with a trailing slash; that must not produce a
    double slash the gateway answers with 404."""
    return f"{base_url.rstrip('/')}{path}"


def token_count(value: Any) -> int:
    """A usage number that a gateway may omit, null, or send as a float."""
    return int(value) if isinstance(value, int | float) else 0


def model_ids(payload: dict[str, Any]) -> list[str]:
    """`{"data": [{"id": ...}]}` — the one thing both formats spell the same."""
    entries = payload.get("data") or []
    ids = [
        entry["id"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    ]
    return sorted(set(ids))
