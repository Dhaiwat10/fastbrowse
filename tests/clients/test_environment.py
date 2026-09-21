from typing import Any

import httpx
import pytest

from fastbrowse.clients.environment import ConfigurationError, JevSource, Settings
from fastbrowse.jev import JEV_MODEL, JevError, JevRetriesExhausted, NoulQuestion


@pytest.fixture(autouse=True)
def no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TYPESAFE_API_KEY",
        "AI_GATEWAY_API_KEY",
        "FASTBROWSE_JEV_SOURCE",
        "FASTBROWSE_JEV_BASE_URL",
        "FASTBROWSE_JEV_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("values", "expected_url"),
    [
        ({"TYPESAFE_API_KEY": "t", "AI_GATEWAY_API_KEY": "g"}, "https://api.typesafe.ai/v1/systemone"),
        (
            {"TYPESAFE_API_KEY": "t", "AI_GATEWAY_API_KEY": "g", "jev_source": JevSource.GATEWAY},
            "https://ai-gateway.vercel.sh/v4/ai/evaluation-model",
        ),
        ({"AI_GATEWAY_API_KEY": "g", "jev_base_url": "http://proxy:8080/"}, "http://proxy:8080/v4/ai/evaluation-model"),
        ({"TYPESAFE_API_KEY": "t", "jev_base_url": "http://proxy:8080"}, "http://proxy:8080/v1/systemone"),
    ],
)
async def test_jev_requests_go_to_the_chosen_source(values: dict[str, object], expected_url: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(400, json={"error": "stop"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        jev = settings(**values).jev(http)
        with pytest.raises(JevError):
            await jev.evaluate("page", {"q": NoulQuestion(instructions="Is it?")})
    assert seen == [expected_url]


def test_a_chosen_source_without_its_key_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="TYPESAFE_API_KEY"):
        settings(AI_GATEWAY_API_KEY="g", jev_source=JevSource.TYPESAFE).jev(httpx.AsyncClient())


@pytest.mark.parametrize("source", JevSource)
@pytest.mark.parametrize("restriction", ["one_key", "proxy", "model"])
async def test_failover_requires_both_keys_and_default_routing(source: JevSource, restriction: str) -> None:
    values = {
        "TYPESAFE_API_KEY": "t" if restriction != "one_key" or source is JevSource.TYPESAFE else None,
        "AI_GATEWAY_API_KEY": "g" if restriction != "one_key" or source is JevSource.GATEWAY else None,
        "jev_source": source,
        "jev_base_url": "https://proxy.test" if restriction == "proxy" else None,
        "jev_model": "custom-jev" if restriction == "model" else JEV_MODEL,
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = settings(**values).jev(http)
        with pytest.raises(JevRetriesExhausted):
            await client.evaluate("page", {"q": NoulQuestion(instructions="Is it?")})
    host = (
        "proxy.test"
        if restriction == "proxy"
        else ("api.typesafe.ai" if source is JevSource.TYPESAFE else "ai-gateway.vercel.sh")
    )
    path = "/v1/systemone" if source is JevSource.TYPESAFE else "/v4/ai/evaluation-model"
    assert seen == [f"https://{host}{path}"] * 6
