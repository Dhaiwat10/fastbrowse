"""Settings from the environment and `.env`, and the default Jev and LLM clients built from them.

Jev: TYPESAFE_API_KEY (direct) or AI_GATEWAY_API_KEY (Vercel AI Gateway); with both set, direct wins
unless FASTBROWSE_JEV_SOURCE picks one (typesafe or gateway). FASTBROWSE_JEV_BASE_URL points either at a
proxy or another host serving the same API, and FASTBROWSE_JEV_MODEL pins a direct-API model version.
LLM: OPENROUTER_API_KEY.
Cloud browser: BROWSER_USE_API_KEY. FASTBROWSE_LLM_MODEL overrides every purpose at once, and
FASTBROWSE_LLM_MODEL_<PURPOSE> (PLAN, READ, FIELD_TEXT, RECOVER, COMPOSE, VERIFY, SHORTCUT) overrides one.
FASTBROWSE_LLM_REASONING sets the reasoning effort: low (default), medium or high. FASTBROWSE_CHROME
names the Chrome binary. `.env.example` lists them all. A real environment variable beats `.env`.
"""

from enum import StrEnum
from typing import assert_never

import httpx
from pydantic import AliasChoices, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastbrowse.clients.openai_compatible import OpenAICompatibleLLM, ReasoningEffort
from fastbrowse.clients.typesafe import TYPESAFE_URL, TypeSafeJevClient
from fastbrowse.clients.vercel import GATEWAY_URL, VercelGatewayJevClient
from fastbrowse.jev import JEV_MODEL, JevClient
from fastbrowse.llm import LLMClient
from fastbrowse.models import LLMPurpose

# The default is the model the live suite passes on, not the fastest one. `evals.latency` timed the
# candidates on the two request shapes that dominate a run and gemini-3.5-flash-lite won both by a
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

# SHORTCUT shares that asymmetry: its only output is an address code confines to the start origin, and a
# wrong one costs a page load and a BACK, not a conclusion. It runs while the start page loads, so its
# latency is hidden only while it stays under a page load.
#
# PLAN joined them once it was written from the task alone and asked for outcomes only. Flash-lite then
# wrote the same requirements as the default on the six live tasks and on compound probes ("find the price
# and email it to Alice" keeps the email), in 0.7s against 1.5 to 8.5s, and went 12/12 live. A lookup waits
# on the plan before its first read, so that spread was wall time. A dropped requirement is still caught
# downstream: the done check and VERIFY judge the task text itself, not the plan.
DEFAULT_MODELS = dict.fromkeys(LLMPurpose, DEFAULT_LLM) | {
    LLMPurpose.FIELD_TEXT: FIELD_TEXT_LLM,
    LLMPurpose.SHORTCUT: FIELD_TEXT_LLM,
    LLMPurpose.PLAN: FIELD_TEXT_LLM,
}

# gemini-3.8-flash reasons before every answer unless told otherwise, and cannot be told not to: it
# rejects reasoning disabled outright. It can be told to reason less. Measured on the plan and read
# request shapes, the default spent 150 to 275 hidden tokens planning, and `low` took plan from 4.6s to
# 3.2s and read from 2.4s to 2.1s on the same model. FASTBROWSE_LLM_REASONING overrides it.
DEFAULT_REASONING = ReasoningEffort.LOW


class JevSource(StrEnum):
    TYPESAFE = "typesafe"
    GATEWAY = "gateway"


class ConfigurationError(RuntimeError):
    """A missing key or an invalid setting in the environment, reported without a traceback."""


def _key(name: str) -> SecretStr | None:
    return Field(default=None, validation_alias=AliasChoices(name))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FASTBROWSE_", extra="ignore", frozen=True)

    typesafe_api_key: SecretStr | None = _key("TYPESAFE_API_KEY")
    ai_gateway_api_key: SecretStr | None = _key("AI_GATEWAY_API_KEY")
    openrouter_api_key: SecretStr | None = _key("OPENROUTER_API_KEY")
    browser_use_api_key: SecretStr | None = _key("BROWSER_USE_API_KEY")
    jev_source: JevSource | None = None
    jev_base_url: str | None = None
    jev_model: str = JEV_MODEL
    llm_model: str | None = None
    llm_model_plan: str | None = None
    llm_model_read: str | None = None
    llm_model_field_text: str | None = None
    llm_model_recover: str | None = None
    llm_model_compose: str | None = None
    llm_model_verify: str | None = None
    llm_model_shortcut: str | None = None
    llm_reasoning: ReasoningEffort = DEFAULT_REASONING
    chrome: str | None = None

    def models(self) -> dict[LLMPurpose, str]:
        """Per-purpose models, most specific setting winning."""
        return {
            purpose: self._purpose_model(purpose) or self.llm_model or DEFAULT_MODELS[purpose] for purpose in LLMPurpose
        }

    def _purpose_model(self, purpose: LLMPurpose) -> str | None:
        match purpose:
            case LLMPurpose.PLAN:
                return self.llm_model_plan
            case LLMPurpose.READ:
                return self.llm_model_read
            case LLMPurpose.FIELD_TEXT:
                return self.llm_model_field_text
            case LLMPurpose.RECOVER:
                return self.llm_model_recover
            case LLMPurpose.COMPOSE:
                return self.llm_model_compose
            case LLMPurpose.VERIFY:
                return self.llm_model_verify
            case LLMPurpose.SHORTCUT:
                return self.llm_model_shortcut
            case _:
                assert_never(purpose)

    def jev(self, http: httpx.AsyncClient) -> JevClient:
        source = self.jev_source or (JevSource.TYPESAFE if self.typesafe_api_key else JevSource.GATEWAY)
        match source:
            case JevSource.TYPESAFE:
                if not self.typesafe_api_key:
                    raise ConfigurationError("set TYPESAFE_API_KEY for Jev, or AI_GATEWAY_API_KEY for the gateway")
                return TypeSafeJevClient(
                    self.typesafe_api_key.get_secret_value(),
                    http=http,
                    base_url=self.jev_base_url or TYPESAFE_URL,
                    model=self.jev_model,
                )
            case JevSource.GATEWAY:
                if not self.ai_gateway_api_key:
                    raise ConfigurationError("set AI_GATEWAY_API_KEY for Jev, or TYPESAFE_API_KEY for the direct API")
                return VercelGatewayJevClient(
                    self.ai_gateway_api_key.get_secret_value(), http=http, base_url=self.jev_base_url or GATEWAY_URL
                )
            case _:
                assert_never(source)

    def llm(self, http: httpx.AsyncClient) -> LLMClient:
        return OpenAICompatibleLLM(
            self.openrouter_key(),
            http=http,
            base_url="https://openrouter.ai/api/v1",
            models=self.models(),
            reasoning_effort=self.llm_reasoning,
        )

    def openrouter_key(self) -> str:
        if not self.openrouter_api_key:
            raise ConfigurationError("set OPENROUTER_API_KEY for the LLM")
        return self.openrouter_api_key.get_secret_value()

    def browser_key(self) -> str:
        if not self.browser_use_api_key:
            raise ConfigurationError("set BROWSER_USE_API_KEY for a cloud browser")
        return self.browser_use_api_key.get_secret_value()


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Name the setting, never echo the input: the field may be a key.
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        raise ConfigurationError(f"invalid settings: {fields}") from None
