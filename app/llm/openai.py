"""OpenAI-compatible wire format. DeepSeek, Ollama and most relay gateways
speak this one, so it is the default.

Only the six differences of design §3 live here (plus token accounting, which
the starvation check needs). Everything else is in `client.py`.
"""

from typing import Any, ClassVar

from app.llm.base import LlmRequest, join_url, model_ids, token_count


class OpenAiAdapter:
    wire_format: ClassVar[str] = "openai"

    def chat_url(self, base_url: str) -> str:
        return join_url(base_url, "/chat/completions")

    def models_url(self, base_url: str) -> str:
        return join_url(base_url, "/models")

    def headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def body(self, model: str, request: LlmRequest) -> dict[str, Any]:
        content: str | list[dict[str, Any]]
        if request.images:
            content = [{"type": "text", "text": request.user_text}]
            content += [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.media_type};base64,{image.b64}"},
                }
                for image in request.images
            ]
        else:
            # A bare string, not a one-element block list: self-hosted servers
            # and relay gateways are likelier to accept the simple shape, and
            # this is the path both scenarios take when send_images is off.
            content = request.user_text
        return {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": content},
            ],
        }

    def text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message") or {}
        return message.get("content") or ""

    def usage(self, payload: dict[str, Any]) -> tuple[int, int]:
        usage = payload.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return token_count(usage.get("completion_tokens")), token_count(
            details.get("reasoning_tokens")
        )

    def models(self, payload: dict[str, Any]) -> list[str]:
        return model_ids(payload)
