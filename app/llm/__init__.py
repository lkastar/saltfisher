"""LLM adapter layer.

Two entry points (market analysis, single-item advice) share one endpoint
config and one adapter layer. The layer is thin on purpose: no vendor SDK,
because the value is that a user can point this at a local Ollama, a relay
gateway or a self-hosted vLLM, and an SDK makes that harder rather than
easier.

`llm/` imports nothing from `app/collector/`. That is a security constraint,
not tidiness: the collector's httpx client carries the goofish cookie jar, and
sending it to a third-party endpoint would hand over the user's logged-in
session. The HTTP client is always passed in by the caller
(`app.state.notify_client`).
"""
