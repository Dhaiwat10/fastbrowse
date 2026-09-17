"""Build the default Jev and LLM clients from environment variables.

Jev: TYPESAFE_API_KEY (direct) or AI_GATEWAY_API_KEY (Vercel AI Gateway).
LLM: OPENROUTER_API_KEY. FASTBROWSE_LLM_MODEL overrides every purpose at once, and
FASTBROWSE_LLM_MODEL_<PURPOSE> (PLAN, READ, FIELD_TEXT, RECOVER, COMPOSE, VERIFY) overrides one.
"""

import os

import httpx

from fastbrowse.clients.openai_compatible import OpenAICompatibleLLM
from fastbrowse.clients.typesafe import TypeSafeJevClient
from fastbrowse.clients.vercel import VercelGatewayJevClient
from fastbrowse.jev import JevClient
from fastbrowse.llm import LLMClient
from fastbrowse.models import LLMPurpose

# Measured, not assumed: `fastbrowse.evals.latency` timed 17 candidates on the two request shapes
# that dominate a run, and this one was fastest on both by a wide margin (plan 1.6s, read 0.6s,
# against 5.9s and 1.7s for gemini-3.8-flash). Re-measure before changing it; the ranking moves.
FAST_LLM = "google/gemini-3.5-flash-lite"

# The plan is the one call that does not only produce output for us: its requirements and subgoals
# become part of the state every subsequent Jev question is asked against. A vaguer plan makes Jev
# less certain, and a live run spent that as two `uncertain next step (0.45)` stalls on pages it had
# already navigated to correctly. So the planner keeps the stronger model and the inner loop, which
# runs many times per task and only has to read what is in front of it, takes the fast one.
PLANNER_LLM = "google/gemini-3.8-flash"

DEFAULT_MODELS = {purpose: FAST_LLM for purpose in LLMPurpose} | {LLMPurpose.PLAN: PLANNER_LLM}


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
        key, http=http, base_url="https://openrouter.ai/api/v1", models=models_from_environment()
    )
