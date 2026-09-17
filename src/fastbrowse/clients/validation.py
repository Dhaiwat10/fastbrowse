"""Shared wire encoding and strict validation for both Jev transports."""

import asyncio
import math
from collections.abc import Mapping
from typing import assert_never

import httpx
from pydantic import JsonValue, TypeAdapter

from fastbrowse.jev import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    JevError,
    JevInputTooLarge,
    NoulAnswer,
    NoulQuestion,
    Question,
    ScoreAnswer,
    ScoreQuestion,
)
from fastbrowse.models import CostBasis, CostComponent, CostLine


def wire_questions(questions: Mapping[str, Question], *, gateway: bool = False) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, question in questions.items():
        body: dict[str, JsonValue] = {"instructions": question.instructions}
        match question:
            case ChoiceQuestion():
                body.update(type="choice", criteria=dict(question.criteria))
            case NoulQuestion():
                body["type"] = "boolean" if gateway else "noul"
                criteria: dict[str, JsonValue] = {}
                if question.true is not None:
                    criteria["true"] = question.true
                if question.false is not None:
                    criteria["false"] = question.false
                if criteria:
                    body["criteria"] = criteria
            case ScoreQuestion():
                body.update(type="score", criteria=list(question.criteria))
            case _:
                assert_never(question)
        result[key] = body
    return result


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def number(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("expected a finite number")
    return float(value)


def probability(value: JsonValue) -> float:
    result = number(value)
    if not 0 <= result <= 1:
        raise ValueError("probability outside [0, 1]")
    return result


def token_count(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a nonnegative integer token count")
    return value


def dollars(value: JsonValue) -> float:
    result = float(value) if isinstance(value, str) else number(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("expected a finite nonnegative cost")
    return result


def json_object(response: httpx.Response) -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_json(response.content)


def response_error(response: httpx.Response, detail: str) -> JevError:
    return JevError(f"{detail[:300]}; HTTP {response.status_code}: {response.text[:400]}")


RETRY_DELAYS_SECONDS = (0.5, 1.5, 4.0)
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


async def post_with_retry(
    http: httpx.AsyncClient, url: str, body: dict[str, JsonValue], headers: Mapping[str, str]
) -> httpx.Response | None:
    """Retry an overloaded or dropped request, which produced nothing and is always safe to repeat.

    Returns None when the transport never completed, leaving each client to name its own failure.
    """
    for delay in RETRY_DELAYS_SECONDS:
        try:
            response = await http.post(url, json=body, headers=headers)
        except httpx.HTTPError:
            await asyncio.sleep(delay)
            continue
        if response.status_code not in RETRYABLE_STATUS:
            return response
        await asyncio.sleep(delay)
    try:
        return await http.post(url, json=body, headers=headers)
    except httpx.HTTPError:
        return None


async def post(
    http: httpx.AsyncClient,
    url: str,
    api_key: str,
    body: dict[str, JsonValue],
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    response = await post_with_retry(http, url, body, {"Authorization": f"Bearer {api_key}", **(headers or {})})
    if response is None:
        raise JevError("Jev transport failed")
    if response.status_code == 400 and "max_tokens_exceeded" in response.text:
        raise JevInputTooLarge(f"Jev input too large; HTTP 400: {response.text[:400]}")
    if not response.is_success:
        raise response_error(response, "Jev request failed")
    return response


def _probabilities(value: JsonValue, keys: set[str]) -> dict[str, float]:
    raw = object_value(value)
    if not keys or set(raw) != keys:
        raise ValueError("probability keys differ from criteria")
    result = {key: probability(value) for key, value in raw.items()}
    # Two-decimal wire rounding can put a valid distribution exactly on the tolerance boundary.
    if abs(math.fsum(result.values()) - 1) > 0.02 + 1e-12:
        raise ValueError("probabilities do not sum to one within 0.02")
    return result


def parse_answers(
    value: JsonValue,
    questions: Mapping[str, Question],
    *,
    gateway: bool = False,
    confidence: Mapping[str, JsonValue] | None = None,
) -> dict[str, Answer]:
    raw = object_value(value)
    if set(raw) != set(questions):
        raise ValueError("answer ids differ from question ids")
    answers: dict[str, Answer] = {}
    for key, question in questions.items():
        answer = object_value(raw[key])
        expected_type = "boolean" if gateway and isinstance(question, NoulQuestion) else question.type
        if answer.get("type") != expected_type:
            raise ValueError(f"incorrect answer type for {key}")
        confidence_value = (confidence or {}).get(key) if gateway else answer.get("confidence")
        match question:
            case ChoiceQuestion():
                probs = _probabilities(answer.get("probabilities"), set(question.criteria))
                choice = answer.get("choice")
                if not isinstance(choice, str) or choice not in probs:
                    raise ValueError(f"choice outside criteria for {key}")
                if probs[choice] < max(probs.values()) - 1e-6:
                    raise ValueError(f"chosen option is not maximal for {key}")
                answers[key] = ChoiceAnswer(
                    choice=choice, probabilities=probs, confidence=probability(confidence_value)
                )
            case NoulQuestion():
                answers[key] = NoulAnswer(probability=probability(answer.get("probability")))
            case ScoreQuestion():
                probs = _probabilities(answer.get("probabilities"), {str(i) for i in range(len(question.criteria))})
                score = number(answer.get("score"))
                if not 0 <= score <= len(question.criteria) - 1:
                    raise ValueError(f"score outside levels for {key}")
                answers[key] = ScoreAnswer(score=score, probabilities=probs, confidence=probability(confidence_value))
            case _:
                assert_never(question)
    return answers


def estimated_cost(input_tokens: int, output_tokens: int = 0) -> CostLine:
    return CostLine(
        component=CostComponent.JEV,
        basis=CostBasis.ESTIMATED,
        dollars=input_tokens * 0.042 / 1_000_000,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
