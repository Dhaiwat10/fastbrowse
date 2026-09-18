"""A remote Chrome from Browser Use Cloud, with its metered browser and proxy cost.

async with BrowserUseCloudBrowser(api_key, http=http) as cloud:
    async with BrowserSession(cloud.connection, sink) as session: ...
cloud.cost  # metered lines, available after exit
"""

import asyncio
from contextlib import suppress
from decimal import Decimal
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

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
        self._browser_id: str | None = None
        self._connection: BrowserConnection | None = None
        self.cost: tuple[CostLine, ...] = ()

    @property
    def connection(self) -> BrowserConnection:
        if self._connection is None:
            raise BrowserUseCloudError("the cloud browser is not running")
        return self._connection

    async def __aenter__(self) -> Self:
        creation = asyncio.create_task(self._create())
        try:
            # A cancelled POST can still create a billable browser; wait until its id is known.
            browser = await asyncio.shield(creation)
            if browser.cdp_url is None:
                raise BrowserUseCloudError(f"browser {browser.id} started without a CDP URL")
            version = await self._http.get(f"{browser.cdp_url}/json/version")
            version.raise_for_status()
            ws_url = str(version.json()["webSocketDebuggerUrl"])
            self._connection = BrowserConnection(cdp_url=ws_url, live_url=browser.live_url, remote=True)
        except BaseException:
            await asyncio.gather(creation, return_exceptions=True)
            with suppress(BaseException):
                await self._finish()
            raise
        return self

    async def _create(self) -> _BrowserView:
        created = await self._call("POST", "/browsers", json=self._body)
        raw: JsonValue = created.json()
        # Validation of optional fields must not lose the id needed for rollback.
        if isinstance(raw, dict) and isinstance(browser_id := raw.get("id"), str):
            self._browser_id = browser_id
        browser = _BrowserView.model_validate(raw)
        self._record_cost(browser)
        return browser

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            await self._finish()
        except BaseException:
            if exc is None:
                raise

    async def _finish(self) -> None:
        stopping = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(stopping)
        finally:
            # The remote browser keeps billing until this request finishes, even after caller cancellation.
            await asyncio.gather(stopping, return_exceptions=True)

    async def _stop(self) -> None:
        """Stop the browser (it bills until stopped or timed out) and record what it cost."""
        if self._browser_id is None:
            return
        self._connection = None
        # Forget the browser only once the stop succeeded, so a failed stop can be retried rather than left billing.
        response = await self._call("PATCH", f"/browsers/{self._browser_id}", json={"action": "stop"})
        self._browser_id = None
        stopped = _BrowserView.model_validate_json(response.content)
        self._record_cost(stopped)

    def _record_cost(self, browser: _BrowserView) -> None:
        self.cost = (
            CostLine(component=CostComponent.BROWSER, basis=CostBasis.METERED, dollars=float(browser.browser_cost)),
            CostLine(component=CostComponent.PROXY, basis=CostBasis.METERED, dollars=float(browser.proxy_cost)),
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
