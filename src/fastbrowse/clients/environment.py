"""Build the default Jev and LLM clients from environment variables.

Jev: TYPESAFE_API_KEY (direct) or AI_GATEWAY_API_KEY (Vercel AI Gateway).
LLM: OPENROUTER_API_KEY, model FASTBROWSE_LLM_MODEL (default below) for every purpose.
"""

import os

import httpx

from fastbrowse.clients.openai_compatible import OpenAICompatibleLLM
from fastbrowse.clients.typesafe import TypeSafeJevClient
from fastbrowse.clients.vercel import VercelGatewayJevClient
from fastbrowse.jev import JevClient
from fastbrowse.llm import LLMClient
from fastbrowse.models import LLMPurpose

DEFAULT_LLM = "google/gemini-3.8-flash"


class MissingKeyError(RuntimeError):
    pass


def jev_from_environment(http: httpx.AsyncClient) -> JevClient:
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return TypeSafeJevClient(key, http=http)
    if key := os.environ.get("AI_GATEWAY_API_KEY"):
        return VercelGatewayJevClient(key, http=http)
    raise MissingKeyError("set TYPESAFE_API_KEY or AI_GATEWAY_API_KEY for Jev")


def llm_from_environment(http: httpx.AsyncClient) -> LLMClient:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise MissingKeyError("set OPENROUTER_API_KEY for the LLM")
    model = os.environ.get("FASTBROWSE_LLM_MODEL", DEFAULT_LLM)
    return OpenAICompatibleLLM(
        key, http=http, base_url="https://openrouter.ai/api/v1", models=dict.fromkeys(LLMPurpose, model)
    )
