"""The six wire-format differences, and the token accounting the starvation
check depends on.

Nothing here touches the network: an adapter is pure, which is why the HTTP
call lives in client.py instead.
"""

import pathlib
import re
from base64 import b64encode

from app.llm.anthropic import API_VERSION, AnthropicAdapter
from app.llm.base import LlmImage, LlmRequest, media_type
from app.llm.openai import OpenAiAdapter

PNG = b64encode(bytes.fromhex("89504e470d0a1a0a") + b"x" * 32).decode()
JPEG = b64encode(bytes.fromhex("ffd8ffe0") + b"x" * 32).decode()

openai = OpenAiAdapter()
anthropic = AnthropicAdapter()


def request(**kw) -> LlmRequest:
    return LlmRequest(**{"system": "你是助手", "user_text": "报个价", **kw})


# --------------------------------------------------------------------------- #
# 1 & 6: paths
# --------------------------------------------------------------------------- #


def test_paths_differ_between_formats():
    assert openai.chat_url("https://api.deepseek.com") == (
        "https://api.deepseek.com/chat/completions"
    )
    assert openai.models_url("https://api.deepseek.com") == "https://api.deepseek.com/models"
    assert (
        anthropic.chat_url("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages"
    )
    assert (
        anthropic.models_url("https://api.anthropic.com") == "https://api.anthropic.com/v1/models"
    )


def test_pasted_trailing_slash_does_not_double_up():
    """A user pastes the URL from a docs page; the extra slash is a 404 on
    several gateways and reads like a wrong base_url."""
    assert openai.chat_url("https://api.deepseek.com/v1/") == (
        "https://api.deepseek.com/v1/chat/completions"
    )
    assert anthropic.models_url("https://gw.example.com/") == "https://gw.example.com/v1/models"


# --------------------------------------------------------------------------- #
# 2: auth headers
# --------------------------------------------------------------------------- #


def test_auth_headers_differ():
    assert openai.headers("sk-secret")["Authorization"] == "Bearer sk-secret"
    assert "x-api-key" not in openai.headers("sk-secret")

    headers = anthropic.headers("sk-secret")
    assert headers["x-api-key"] == "sk-secret"
    assert headers["anthropic-version"] == API_VERSION
    assert "Authorization" not in headers


# --------------------------------------------------------------------------- #
# 3: where the system prompt goes
# --------------------------------------------------------------------------- #


def test_openai_puts_system_in_messages():
    body = openai.body("deepseek-v4-flash", request())
    assert body["messages"][0] == {"role": "system", "content": "你是助手"}
    assert body["messages"][1] == {"role": "user", "content": "报个价"}
    assert "system" not in body


def test_anthropic_puts_system_at_top_level():
    body = anthropic.body("claude-x", request())
    assert body["system"] == "你是助手"
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_max_tokens_travels_in_both():
    assert openai.body("m", request(max_tokens=1500))["max_tokens"] == 1500
    assert anthropic.body("m", request(max_tokens=1500))["max_tokens"] == 1500


# --------------------------------------------------------------------------- #
# 4: image blocks
# --------------------------------------------------------------------------- #


def test_openai_image_is_a_data_url():
    body = openai.body("m", request(images=(LlmImage("image/png", PNG),)))
    blocks = body["messages"][1]["content"]
    assert blocks[0] == {"type": "text", "text": "报个价"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"] == f"data:image/png;base64,{PNG}"


def test_anthropic_image_is_a_base64_source():
    body = anthropic.body("m", request(images=(LlmImage("image/jpeg", JPEG),)))
    blocks = body["messages"][1 - 1]["content"]
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": JPEG},
    }


def test_the_declared_mime_wins_over_what_the_bytes_look_like():
    """The label comes from the download's Content-Type, not from sniffing.

    Passing PNG bytes labelled webp proves the adapter forwards what it was
    told: re-deriving the mime here would silently rewrite it to image/png and
    lose whatever the server actually said, and `media_type()` cannot see a
    format whose magic bytes are not in its table.
    """
    body = openai.body("m", request(images=(LlmImage("image/webp", PNG),)))
    url = body["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/webp;base64,")


def test_no_images_means_a_plain_string_content():
    """Relay gateways and local servers are likelier to accept the simple
    shape, and this is the path both scenarios take with send_images off."""
    assert openai.body("m", request())["messages"][1]["content"] == "报个价"
    assert anthropic.body("m", request())["messages"][0]["content"] == "报个价"


def test_media_type_is_sniffed_from_the_base64():
    """LlmRequest carries bare base64, so the mime has to come from the bytes.
    jpeg is the default because the goofish CDN serves jpg for photos."""
    assert media_type(PNG) == "image/png"
    assert media_type(JPEG) == "image/jpeg"
    assert media_type(b64encode(b"RIFF\x00\x00\x00\x00WEBPVP8 ").decode()) == "image/webp"
    assert media_type("nonsense") == "image/jpeg"


# --------------------------------------------------------------------------- #
# 5: where the answer is
# --------------------------------------------------------------------------- #


def test_openai_reads_the_message_content():
    payload = {"choices": [{"message": {"role": "assistant", "content": "red blue"}}]}
    assert openai.text(payload) == "red blue"


def test_anthropic_reads_text_blocks_and_ignores_thinking():
    payload = {
        "content": [
            {"type": "thinking", "thinking": "内部推理，不该展示"},
            {"type": "text", "text": "red blue"},
        ]
    }
    assert anthropic.text(payload) == "red blue"


def test_a_response_without_text_parses_as_empty_not_as_a_crash():
    """The measured starvation case: HTTP 200, finish_reason stop, no content.
    Raising here would report a transport failure for a healthy call."""
    assert openai.text({"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}) == ""
    assert openai.text({"choices": []}) == ""
    assert openai.text({}) == ""
    assert anthropic.text({"content": [{"type": "thinking", "thinking": "…"}]}) == ""
    assert anthropic.text({}) == ""


# --------------------------------------------------------------------------- #
# Token accounting (the input to `starved`)
# --------------------------------------------------------------------------- #


def test_openai_usage_reads_the_reasoning_breakdown():
    """The real numbers from the probe: max_tokens=60 spent all 60 thinking."""
    payload = {
        "usage": {
            "prompt_tokens": 219,
            "completion_tokens": 60,
            "completion_tokens_details": {"reasoning_tokens": 60},
        }
    }
    assert openai.usage(payload) == (60, 60)


def test_openai_usage_survives_a_gateway_that_omits_the_details():
    assert openai.usage({"usage": {"completion_tokens": 13}}) == (13, 0)
    assert openai.usage({}) == (0, 0)


def test_anthropic_charges_invisible_output_to_reasoning():
    """This format reports no reasoning breakdown. Tokens produced with no
    text block means the budget went somewhere invisible, which has to be
    reported as starvation or the user is told to fix the wrong thing."""
    thinking_only = {
        "content": [{"type": "thinking", "thinking": "…"}],
        "usage": {"output_tokens": 60},
    }
    assert anthropic.usage(thinking_only) == (60, 60)

    answered = {"content": [{"type": "text", "text": "ok"}], "usage": {"output_tokens": 13}}
    assert anthropic.usage(answered) == (13, 0)


# --------------------------------------------------------------------------- #
# Model lists
# --------------------------------------------------------------------------- #


def test_both_formats_parse_the_model_list():
    payload = {
        "data": [
            {"id": "deepseek-v4-pro"},
            {"id": "deepseek-v4-flash"},
            {"id": "deepseek-v4-flash-vision-exp"},
            {"id": ""},
            {"no_id": True},
            "junk",
        ]
    }
    expected = ["deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro"]
    assert openai.models(payload) == expected
    assert anthropic.models(payload) == expected
    assert openai.models({}) == []


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #


def test_llm_never_imports_the_collector():
    """`MtopClient`'s httpx client carries the goofish cookie jar. Reusing it
    would send the user's logged-in session to a third-party endpoint, so the
    layer is kept unable to reach it rather than merely told not to.
    """
    imports = re.compile(r"^\s*(?:from|import)\s+\S*collector", re.MULTILINE)
    for source in pathlib.Path("app/llm").glob("*.py"):
        assert not imports.search(source.read_text()), source
