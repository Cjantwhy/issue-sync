"""Classifier backed by any OpenAI-compatible chat completions API.

Configured entirely by environment variables (base URL, model, key), so switching
providers needs no code change. Provider-specific options (e.g. DeepSeek's
`{"thinking": {"type": "disabled"}}`) go into `LLM_EXTRA_BODY`.
"""

import logging
from typing import Any, Literal

import httpx

from issue_sync.adapter_config import ClassifierConfig
from issue_sync.classify.base import Classification, ClassifierError, ClassifierInput
from issue_sync.classify.prompt import parse_judgement, response_schema, system_prompt, user_prompt
from issue_sync.settings import LlmSettings

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 1000
MODEL_NAME_MAX_LENGTH = 100  # ai_judgement.model column size

ResponseFormat = Literal["json_object", "json_schema"]


class OpenAICompatibleClassifier:
    def __init__(
        self,
        http: httpx.AsyncClient,
        config: ClassifierConfig,
        *,
        base_url: str,
        api_key: str,
        model: str,
        response_format: ResponseFormat = "json_object",
        extra_body: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._http = http
        self._config = config
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._response_format = response_format
        self._extra_body = extra_body or {}
        self._timeout = timeout
        self._system_prompt = system_prompt(config)

    async def classify(self, request: ClassifierInput) -> Classification:
        body = await self._post(self._request_body(request))
        try:
            choice = body["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ClassifierError("unexpected response shape", retryable=True) from None
        if choice.get("finish_reason") == "length":
            raise ClassifierError("response was cut off (max tokens reached)", retryable=True)
        try:
            judgement = parse_judgement(content, self._config)
        except ValueError as exc:
            # Sampling can produce a bad answer once; another attempt often succeeds.
            raise ClassifierError(f"invalid model output: {exc}", retryable=True) from None
        model = str(body.get("model") or self._model)[:MODEL_NAME_MAX_LENGTH]
        usage = body.get("usage") or {}
        logger.info(
            "classified with %s: %s/%s/%s (tokens in=%s out=%s)",
            model,
            judgement.type,
            judgement.urgency,
            judgement.assignee_role,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        return Classification(judgement=judgement, model=model)

    def _request_body(self, request: ClassifierInput) -> dict[str, Any]:
        if self._response_format == "json_schema":
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "triage",
                    "strict": True,
                    "schema": response_schema(self._config),
                },
            }
        else:
            response_format = {"type": "json_object"}
        return {
            **self._extra_body,
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_prompt(request)},
            ],
            "response_format": response_format,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0,
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ClassifierError("LLM request timed out", retryable=True) from None
        except httpx.TransportError as exc:
            raise ClassifierError(f"LLM request failed: {exc!r}", retryable=True) from None
        if response.is_success:
            try:
                return response.json()
            except ValueError:
                raise ClassifierError("LLM returned non-JSON body", retryable=True) from None
        status = response.status_code
        message = f"LLM API returned {status}: {response.text[:300]}"
        if status == 429 or status >= 500:
            retry_after = response.headers.get("Retry-After")
            raise ClassifierError(
                message,
                retryable=True,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        # 400 (bad request), 401/403 (key), 402 (balance): a retry cannot fix these.
        raise ClassifierError(message)


def create_classifier(
    settings: LlmSettings, config: ClassifierConfig, http: httpx.AsyncClient
) -> OpenAICompatibleClassifier:
    return OpenAICompatibleClassifier(
        http,
        config,
        base_url=str(settings.llm_base_url),
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        response_format=settings.llm_response_format,
        extra_body=settings.llm_extra_body,
        timeout=settings.llm_timeout_seconds,
    )
