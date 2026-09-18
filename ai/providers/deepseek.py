"""Direct DeepSeek adapter for pattern edits.

Server-side only: the API key never leaves the process, is never logged, and is
never sent to a client. The adapter validates the model's structured response
and records the requested/returned model so an API alias change stays visible.

The API model id ``deepseek-flash`` was verified against the official release
documentation on 2026-09-18 (https://api-docs.deepseek.com/news/news260910/).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

from core.pattern_edit_store import EditError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
MAX_OUTPUT_BYTES = 512 * 1024


class ProviderError(EditError):
    """Provider call failed. ``retryable`` marks transient transport/5xx/429."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ProviderUnavailable(ProviderError):
    pass


@dataclass(frozen=True)
class EditGenerationRequest:
    instruction: str
    pattern_id: str
    source_path: str
    documentation_path: str
    source: str
    documentation: str
    interface: dict
    context: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EditGeneration:
    source: str
    documentation: str
    explanation: str
    requested_model: str
    returned_model: str | None = None
    request_id: str | None = None
    usage: dict = field(default_factory=dict)


class EditProvider(Protocol):
    def available(self) -> bool: ...

    def generate(self, request: EditGenerationRequest) -> EditGeneration: ...


SYSTEM_PROMPT = (
    "You edit one chart-pattern detector and its paired Markdown documentation. "
    "Return ONLY a JSON object with exactly these string keys: "
    '"source" (the complete replacement Python module), '
    '"documentation" (the complete replacement Markdown document) and '
    '"explanation" (a short plain-language summary of the change). '
    "Keep the detector class name, the pattern identity and every TradeSignal "
    "field name unchanged. Do not add imports outside the standard library and "
    "the pattern interface already shown. Do not include any other keys."
)


def _user_prompt(request: EditGenerationRequest) -> str:
    lines = [
        f"Pattern id: {request.pattern_id}",
        f"Editable detector file: {request.source_path}",
        f"Editable documentation file: {request.documentation_path}",
        "Detector interface contract: " + json.dumps(request.interface, sort_keys=True),
        f"Instruction: {request.instruction}",
        "",
        f"--- {request.source_path} (current) ---",
        request.source,
        f"--- {request.documentation_path} (current) ---",
        request.documentation,
    ]
    for path, content in request.context:
        lines += [f"--- {path} (read-only context) ---", content]
    return "\n".join(lines)


class DeepSeekProvider:
    """Minimal, bounded direct client. ``client`` is injectable for tests."""

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: float | None = None,
                 max_retries: int | None = None,
                 max_concurrency: int | None = None,
                 max_output_bytes: int = MAX_OUTPUT_BYTES,
                 client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        from config import settings

        self._api_key = settings.deepseek_api_key if api_key is None else api_key
        self.base_url = (base_url or settings.deepseek_base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or settings.deepseek_model or DEFAULT_MODEL
        self.timeout = float(timeout or settings.deepseek_timeout_seconds)
        self.max_retries = int(
            settings.deepseek_max_retries if max_retries is None else max_retries)
        self.max_concurrency = max(1, int(
            settings.deepseek_max_concurrency if max_concurrency is None
            else max_concurrency))
        self.max_output_bytes = int(max_output_bytes)
        self._slots = threading.BoundedSemaphore(self.max_concurrency)
        self._client = client
        self._sleep = sleep

    def available(self) -> bool:
        return bool(self._api_key and self.base_url and self.model)

    def _post(self, payload: dict) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._client is not None:
            return self._client.post(f"{self.base_url}/chat/completions",
                                     json=payload, headers=headers)
        with httpx.Client(timeout=self.timeout) as client:
            return client.post(f"{self.base_url}/chat/completions",
                               json=payload, headers=headers)

    def generate(self, request: EditGenerationRequest) -> EditGeneration:
        if not self.available():
            raise ProviderUnavailable(
                "DeepSeek credentials/base URL are not configured", retryable=False)
        # Bound concurrent provider calls; the slot is held across retries so a
        # slow/retrying call cannot be amplified past the configured limit.
        with self._slots:
            return self._generate(request)

    def _generate(self, request: EditGenerationRequest) -> EditGeneration:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(request)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "stream": False,
        }
        last: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._post(payload)
            except httpx.HTTPError as exc:
                last = ProviderError(f"DeepSeek transport error: {type(exc).__name__}",
                                     retryable=True)
            else:
                if response.status_code == 200:
                    return self._parse(response)
                retryable = response.status_code == 429 or response.status_code >= 500
                last = ProviderError(
                    f"DeepSeek returned HTTP {response.status_code}", retryable=retryable)
                if not retryable:
                    raise last
            if attempt < self.max_retries:
                self._sleep(min(2 ** attempt, 8))
        raise last or ProviderError("DeepSeek call failed", retryable=True)

    def _parse(self, response: httpx.Response) -> EditGeneration:
        raw = response.content
        if len(raw) > self.max_output_bytes:
            raise ProviderError("DeepSeek response exceeded the size limit")
        try:
            body = json.loads(raw)
        except ValueError:
            raise ProviderError("DeepSeek returned a non-JSON envelope") from None
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError("DeepSeek returned no choices")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("DeepSeek returned an empty message")
        try:
            data = json.loads(content)
        except ValueError:
            raise ProviderError("DeepSeek did not return a JSON object") from None
        if not isinstance(data, dict):
            raise ProviderError("DeepSeek response must be a JSON object")
        extra = set(data) - {"source", "documentation", "explanation"}
        if extra:
            raise ProviderError("DeepSeek returned unexpected keys: " + ", ".join(sorted(extra)))
        missing = [key for key in ("source", "documentation", "explanation")
                   if not isinstance(data.get(key), str) or not data[key].strip()]
        if missing:
            raise ProviderError("DeepSeek response missing usable: " + ", ".join(missing))
        return EditGeneration(
            source=data["source"],
            documentation=data["documentation"],
            explanation=data["explanation"].strip(),
            requested_model=self.model,
            returned_model=body.get("model") or self.model,
            request_id=body.get("id"),
            usage=body.get("usage") or {},
        )
