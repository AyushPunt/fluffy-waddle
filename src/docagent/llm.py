"""LLM gateway: one narrow interface, swappable providers, content-free metadata.

Every call returns hashes of the prompt and output plus the provider's request ID.
Nothing here logs or stores prompt or output text. Models are pinned by ID, and
there is no automatic fallback to another model: a model that has not passed the
golden set must not write GxP content, so a refusal becomes a gap instead.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from docagent.errors import DocAgentError
from docagent.manifest import canonical_sha256

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(DocAgentError):
    """The model provider failed; the run stops rather than guessing."""


@dataclass
class LLMResult:
    data: dict[str, Any] | None  # None when the model refused
    model: str
    prompt_sha256: str
    output_sha256: str
    request_id: str | None
    refused: bool = False

    def meta(self, purpose: str) -> dict[str, Any]:
        return {
            "purpose": purpose,
            "model": self.model,
            "request_id": self.request_id,
            "prompt_sha256": self.prompt_sha256,
            "output_sha256": self.output_sha256,
            "refused": self.refused,
        }


class LLM(Protocol):
    model: str

    def complete_json(
        self, system: list[dict[str, Any]], user: str, schema: dict[str, Any], effort: str
    ) -> LLMResult: ...


def prompt_hash(model: str, system: list[dict[str, Any]], user: str, schema: dict[str, Any], effort: str) -> str:
    return canonical_sha256({"model": model, "system": system, "user": user, "schema": schema, "effort": effort})


class AnthropicLLM:
    """Claude through the Messages API with a JSON-schema constrained response."""

    def __init__(self, model: str = DEFAULT_MODEL, client: Any = None):
        import anthropic

        self.model = model
        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()

    def complete_json(
        self, system: list[dict[str, Any]], user: str, schema: dict[str, Any], effort: str
    ) -> LLMResult:
        a = self._anthropic
        digest = prompt_hash(self.model, system, user, schema, effort)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except a.AuthenticationError as exc:
            raise LLMError("the model provider rejected the credentials; run `ant auth login` or set ANTHROPIC_API_KEY") from exc
        except a.RateLimitError as exc:
            raise LLMError("the model provider is rate limiting this key; retry later") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"the model provider returned HTTP {exc.status_code}") from exc
        except a.APIConnectionError as exc:
            raise LLMError("could not reach the model provider") from exc
        except TypeError as exc:
            if "authentication" not in str(exc):
                raise
            raise LLMError("no Anthropic credentials found; run `ant auth login` or set ANTHROPIC_API_KEY") from exc

        if response.stop_reason == "refusal":
            return LLMResult(None, response.model, digest, "", response._request_id, refused=True)
        if response.stop_reason == "max_tokens":
            raise LLMError("the model response was cut off at max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("the model response was not valid JSON") from exc
        return LLMResult(
            data=data,
            model=response.model,
            prompt_sha256=digest,
            output_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            request_id=response._request_id,
        )
