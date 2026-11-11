"""Gemini adapter: a STUB. It exists so the interface is proven to fit a second provider and so
LLM_PROVIDER=gemini fails clearly instead of falling through to another provider.

Mapping notes for the real implementation (not built yet): Gemini's generateContent takes
`contents` with roles user/model and a separate `systemInstruction`, and returns
`candidates[].content.parts[]` plus `usageMetadata` (promptTokenCount, candidatesTokenCount).
Rate limits arrive as HTTP 429 with a RESOURCE_EXHAUSTED status and a RetryInfo detail. All of
that is translated to the same LLMRequest/LLMResponse and typed errors here; nothing outside
this file would change.
"""

from app.providers.base import AdapterCapabilities, LLMRequest, LLMResponse, ProviderUnavailable

DEFAULT_MODEL = "gemini-stub"


class GeminiAdapter:
    name = "gemini"
    capabilities = AdapterCapabilities()

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.model = model or DEFAULT_MODEL

    def complete(self, req: LLMRequest) -> LLMResponse:
        raise ProviderUnavailable("GeminiAdapter is not implemented yet (interface stub)")
