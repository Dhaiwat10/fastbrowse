"""Build the default Jev and LLM clients from environment variables.

Jev: TYPESAFE_API_KEY (direct) or AI_GATEWAY_API_KEY (Vercel AI Gateway).
LLM: OPENROUTER_API_KEY. FASTBROWSE_LLM_MODEL overrides every purpose at once, and
FASTBROWSE_LLM_MODEL_<PURPOSE> (PLAN, READ, FIELD_TEXT, RECOVER, COMPOSE, VERIFY) overrides one.
"""

import os

import httpx

from fastbrowse.clients.openai_compatible import OpenAICompatibleLLM, ReasoningEffort
from fastbrowse.clients.typesafe import TypeSafeJevClient
from fastbrowse.clients.vercel import VercelGatewayJevClient
from fastbrowse.jev import JevClient
from fastbrowse.llm import LLMClient
from fastbrowse.models import LLMPurpose

# The default is the model the live suite scores 12/12 on, not the fastest one. `evals.latency` timed
# 17 candidates on the two request shapes that dominate a run and gemini-3.5-flash-lite won both by a
# wide margin (plan 1.6s against 5.9s, read 0.6s against 1.7s), which on the local fixtures was free:
# 12/12 at 2.9x the speed. Live sites disagreed. Flash-lite scored 8/12, and a run that answers None
# is not a fast run. Three configurations were measured at 12 runs each and none of them held:
# fast everywhere 8/12, a stronger planner 7/12, a stronger reader 9/12. Twelve runs cannot say which
# purpose is responsible, so the default does not guess. Anyone who wants that trade can take it with
# FASTBROWSE_LLM_MODEL=google/gemini-3.5-flash-lite, which the README documents alongside its cost.
DEFAULT_LLM = "google/gemini-3.8-flash"

# FIELD_TEXT is the exception, because it is the one purpose that mints nothing a conclusion rests on:
# it turns "the password" or "Zurich" into the string to type, and a wrong string fails visibly as an
# action rather than quietly as evidence. Browser Use's jev-ultrafast leans on that same asymmetry,
# using a small model for its only LLM call, which is this one.
FIELD_TEXT_LLM = "google/gemini-3.5-flash-lite"

DEFAULT_MODELS = dict.fromkeys(LLMPurpose, DEFAULT_LLM) | {LLMPurpose.FIELD_TEXT: FIELD_TEXT_LLM}

# gemini-3.8-flash reasons before every answer unless told otherwise, and cannot be told not to: it
# rejects reasoning disabled outright. It can be told to reason less. Measured on the plan and read
# request shapes, the default spent 150 to 275 hidden tokens planning, and `low` took plan from 4.6s to
# 3.2s and read from 2.4s to 2.1s on the same model. FASTBROWSE_LLM_REASONING overrides it.
DEFAULT_REASONING = ReasoningEffort.LOW


class MissingKeyError(RuntimeError):
    pass


def jev_from_environment(http: httpx.AsyncClient) -> JevClient:
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return TypeSafeJevClient(key, http=http)
    if key := os.environ.get("AI_GATEWAY_API_KEY"):
        return VercelGatewayJevClient(key, http=http)
    raise MissingKeyError("set TYPESAFE_API_KEY or AI_GATEWAY_API_KEY for Jev")


def models_from_environment() -> dict[LLMPurpose, str]:
    """Per-purpose models, most specific variable winning."""
    every = os.environ.get("FASTBROWSE_LLM_MODEL")
    return {
        purpose: os.environ.get(f"FASTBROWSE_LLM_MODEL_{purpose.name}") or every or default
        for purpose, default in DEFAULT_MODELS.items()
    }


def llm_from_environment(http: httpx.AsyncClient) -> LLMClient:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise MissingKeyError("set OPENROUTER_API_KEY for the LLM")
    return OpenAICompatibleLLM(
        key,
        http=http,
        base_url="https://openrouter.ai/api/v1",
        models=models_from_environment(),
        reasoning_effort=ReasoningEffort(os.environ.get("FASTBROWSE_LLM_REASONING", DEFAULT_REASONING)),
    )
