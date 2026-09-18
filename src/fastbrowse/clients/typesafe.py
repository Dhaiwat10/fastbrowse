"""Direct TypeSafe System One transport, using caller-owned HTTP resources."""

from collections.abc import Mapping
from time import monotonic

import httpx
from pydantic import JsonValue

from fastbrowse.clients.validation import (
    estimated_cost,
    json_object,
    object_value,
    parse_answers,
    post,
    response_error,
    token_count,
    wire_questions,
)
from fastbrowse.jev import JEV_MODEL, Evaluation, Question


class TypeSafeJevClient:
    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.AsyncClient,
        base_url: str = "https://api.typesafe.ai",
        model: str = JEV_MODEL,
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def evaluate(self, state: JsonValue, questions: Mapping[str, Question]) -> Evaluation:
        started = monotonic()
        response = await post(
            self._http,
            f"{self._base_url}/v1/systemone",
            self._api_key,
            {"model": self._model, "state": state, "questions": wire_questions(questions)},
        )
        try:
            payload = json_object(response)
            usage = object_value(payload.get("usage"))
            tokens = token_count(usage.get("input_tokens"))
            model = payload.get("model", self._model)
            if not isinstance(model, str):
                raise ValueError("invalid model name")
            return Evaluation(
                model=model,
                answers=parse_answers(payload.get("answers"), questions),
                input_tokens=tokens,
                cost=estimated_cost(tokens, token_count(usage.get("output_tokens", 0))).model_copy(
                    update={"seconds": monotonic() - started}
                ),
            )
        except (ValueError, TypeError, OverflowError) as error:
            raise response_error(response, f"Invalid Jev response ({error})") from None
