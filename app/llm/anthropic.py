"""Anthropic wire format.

Same six differences as `openai.py`, mirrored. Kept as a sibling rather than
a branch inside one function because the two shapes disagree about where the
system prompt, the image and the answer live — four `if wire_format ==` in one
body is harder to read than two small classes.
"""

from typing import Any, ClassVar

from app.llm.base import LlmRequest, join_url, model_ids, token_count

# Required header; the API rejects a request without it.
API_VERSION = "2023-06-01"


class AnthropicAdapter:
    wire_format: ClassVar[str] = "anthropic"

    def chat_url(self, base_url: str) -> str:
        return join_url(base_url, "/v1/messages")

    def models_url(self, base_url: str) -> str:
        return join_url(base_url, "/v1/models")

    def headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        }

    def body(self, model: str, request: LlmRequest) -> dict[str, Any]:
        content: str | list[dict[str, Any]]
        if request.images:
            content = [{"type": "text", "text": request.user_text}]
            content += [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": image.b64,
                    },
                }
                for image in request.images
            ]
        else:
            content = request.user_text
        return {
            "model": model,
            "max_tokens": request.max_tokens,
            # Top level, not a message with role "system".
            "system": request.system,
            "messages": [{"role": "user", "content": content}],
        }

    def text(self, payload: dict[str, Any]) -> str:
        blocks = payload.get("content") or []
        return "".join(
            block.get("text") or ""
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

    def usage(self, payload: dict[str, Any]) -> tuple[int, int]:
        usage = payload.get("usage") or {}
        completion = token_count(usage.get("output_tokens"))
        # This format reports no reasoning breakdown: extended thinking comes
        # back as a "thinking" block and is billed inside output_tokens. When
        # tokens were produced but no text block exists, the whole budget went
        # somewhere invisible — the same starvation OpenAI states outright, so
        # it has to be reported the same way or the message misdirects.
        reasoning = completion if completion and not self.text(payload) else 0
        return completion, reasoning

    def models(self, payload: dict[str, Any]) -> list[str]:
        return model_ids(payload)
