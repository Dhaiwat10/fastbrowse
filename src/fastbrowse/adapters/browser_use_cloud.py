"""A remote Chrome from Browser Use Cloud, with its metered browser and proxy cost.

async with BrowserUseCloudBrowser(api_key, http=http) as cloud:
    async with BrowserSession(cloud.connection, sink) as session: ...
cloud.cost  # metered lines, available after exit
"""

from decimal import Decimal
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field

from fastbrowse.models import BrowserConnection, CostBasis, CostComponent, CostLine

API = "https://api.browser-use.com/api/v3"


class _BrowserView(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    cdp_url: str | None = Field(default=None, alias="cdpUrl")
    live_url: str | None = Field(default=None, alias="liveUrl")
    browser_cost: Decimal = Field(default=Decimal(0), alias="browserCost")
    proxy_cost: Decimal = Field(default=Decimal(0), alias="proxyCost")


class BrowserUseCloudError(RuntimeError):
    pass


class BrowserUseCloudBrowser:
    def __init__(
        self, api_key: str, *, http: httpx.AsyncClient, proxy_country: str | None = "us", timeout_minutes: int = 15
    ) -> None:
        self._http = http
        self._headers = {"X-Browser-Use-API-Key": api_key}
        self._body: dict[str, str | int] = {"timeout": timeout_minutes}
        if proxy_country is not None:
            self._body["proxyCountryCode"] = proxy_country
        self._browser: _BrowserView | None = None
        self._connection: BrowserConnection | None = None
        self.cost: tuple[CostLine, ...] = ()

    @property
    def connection(self) -> BrowserConnection:
        if self._connection is None:
            raise BrowserUseCloudError("the cloud browser is not running")
        return self._connection

    async def __aenter__(self) -> Self:
        created = await self._call("POST", "/browsers", json=self._body)
        self._browser = _BrowserView.model_validate_json(created.content)
        try:
            if self._browser.cdp_url is None:
                raise BrowserUseCloudError(f"browser {self._browser.id} started without a CDP URL")
            version = await self._http.get(f"{self._browser.cdp_url}/json/version")
            version.raise_for_status()
            ws_url = str(version.json()["webSocketDebuggerUrl"])
        except (httpx.HTTPError, KeyError, ValueError, BrowserUseCloudError):
            await self._stop()
            raise
        self._connection = BrowserConnection(cdp_url=ws_url, live_url=self._browser.live_url, remote=True)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._stop()

    async def _stop(self) -> None:
        """Stop the browser (it bills until stopped or timed out) and record what it cost."""
        if self._browser is None:
            return
        browser_id, self._browser, self._connection = self._browser.id, None, None
        stopped = _BrowserView.model_validate_json(
            (await self._call("PATCH", f"/browsers/{browser_id}", json={"action": "stop"})).content
        )
        self.cost = (
            CostLine(component=CostComponent.BROWSER, basis=CostBasis.METERED, dollars=float(stopped.browser_cost)),
            CostLine(component=CostComponent.PROXY, basis=CostBasis.METERED, dollars=float(stopped.proxy_cost)),
        )

    async def _call(self, method: str, path: str, *, json: dict[str, str | int]) -> httpx.Response:
        try:
            response = await self._http.request(method, f"{API}{path}", headers=self._headers, json=json)
        except httpx.HTTPError:
            # The request carries the API key header; never let the transport error's request escape.
            raise BrowserUseCloudError(f"Browser Use Cloud {method} {path} failed") from None
        if not response.is_success:
            raise BrowserUseCloudError(f"Browser Use Cloud {method} {path}: HTTP {response.status_code}")
        return response
