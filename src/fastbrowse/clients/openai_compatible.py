"""Structured chat completions with one schema repair and complete usage accounting."""

import base64
from collections.abc import Mapping, Sequence
from typing import assert_never

import httpx
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from fastbrowse.clients.validation import dollars, json_object, object_value, token_count
from fastbrowse.llm import Generation, LLMError, Message
from fastbrowse.models import CostBasis, CostComponent, CostLine, LLMPurpose
from fastbrowse.telemetry import Ledger


def _image_url(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        raise LLMError("Only PNG and JPEG images are supported")
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def _message(message: Message) -> JsonValue:
    if not message.images:
        return {"role": message.role, "content": message.content}
    parts: list[JsonValue] = [{"type": "text", "text": message.content}]
    parts.extend({"type": "image_url", "image_url": {"url": _image_url(image)}} for image in message.images)
    return {"role": message.role, "content": parts}


def _content(payload: dict[str, JsonValue]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("missing completion choices")
    message = object_value(object_value(choices[0]).get("message"))
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("missing completion text (possibly a refusal)")
    return content


def _cost(payload: dict[str, JsonValue], purpose: LLMPurpose) -> CostLine:
    """Never raises: usage we cannot read is an unknown cost, which a dollar cap treats as unaffordable."""
    try:
        usage = object_value(payload.get("usage", {}))
        cost = usage.get("cost")
        return CostLine(
            component=CostComponent.LLM,
            purpose=purpose,
            basis=CostBasis.UNKNOWN if cost is None else CostBasis.METERED,
            dollars=None if cost is None else dollars(cost),
            input_tokens=token_count(usage.get("prompt_tokens", 0)),
            output_tokens=token_count(usage.get("completion_tokens", 0)),
        )
    except (ValueError, TypeError, OverflowError):
        return CostLine(component=CostComponent.LLM, purpose=purpose, basis=CostBasis.UNKNOWN, dollars=None)


def _total_cost(costs: Sequence[CostLine], purpose: LLMPurpose) -> CostLine:
    known = True
    for line in costs:
        match line.basis:
            case CostBasis.METERED:
                pass
            case CostBasis.ESTIMATED | CostBasis.UNKNOWN:
                known = False
            case _:
                assert_never(line.basis)
    return CostLine(
        component=CostComponent.LLM,
        purpose=purpose,
        basis=CostBasis.METERED if known else CostBasis.UNKNOWN,
        dollars=sum(line.dollars or 0 for line in costs) if known else None,
        input_tokens=sum(line.input_tokens for line in costs),
        output_tokens=sum(line.output_tokens for line in costs),
    )


class OpenAICompatibleLLM:
    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.AsyncClient,
        base_url: str,
        models: Mapping[LLMPurpose, str],
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._models = dict(models)

    async def _request(self, body: dict[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            response = await self._http.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
            )
        except httpx.HTTPError:
            raise LLMError("LLM transport failed") from None
        if not response.is_success:
            raise LLMError(f"LLM HTTP {response.status_code}: {response.text[:400]}")
        try:
            return json_object(response)
        except ValueError:
            raise LLMError(f"Invalid LLM response; HTTP {response.status_code}: {response.text[:400]}") from None

    async def generate[T: BaseModel](
        self,
        purpose: LLMPurpose,
        messages: Sequence[Message],
        schema: type[T],
        *,
        max_output_tokens: int = 2000,
        ledger: Ledger | None = None,
    ) -> Generation[T]:
        model = self._models.get(purpose)
        if model is None:
            raise LLMError(f"No model configured for {purpose}")
        wire_messages = [_message(message) for message in messages]
        body: dict[str, JsonValue] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "messages": wire_messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": TypeAdapter(JsonValue).validate_python(schema.model_json_schema()),
                    "strict": False,
                },
            },
        }
        costs: list[CostLine] = []
        for attempt in range(2):
            if ledger is not None:
                ledger.reserve(CostComponent.LLM)
            payload = await self._request(body)
            # Recorded before the envelope is read: a generation we cannot parse was still billed, and
            # dropping it would let an unaccounted request pass a dollar cap.
            costs.append(_cost(payload, purpose))
            try:
                content = _content(payload)
            except (ValueError, TypeError, OverflowError) as error:
                # An empty completion comes back intermittently (a dropped or refused generation); ask once more.
                if attempt == 0:
                    continue
                raise LLMError(f"Invalid completion envelope: {str(error)[:400]}") from None
            try:
                data = schema.model_validate_json(content)
            except ValidationError as error:
                # Avoid echoing rejected values: validation paths and reasons are enough to repair the schema.
                detail = error.json(include_input=False, include_url=False)
                if attempt == 1:
                    raise LLMError(f"LLM schema validation failed after one retry: {detail[:1000]}") from None
                wire_messages.extend(
                    [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": f"Correct the JSON to match the schema. Validation errors:\n{detail}",
                        },
                    ]
                )
            else:
                return Generation(data=data, cost=_total_cost(costs, purpose))
        raise AssertionError("unreachable")
