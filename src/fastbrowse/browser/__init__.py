"""CDP browser layer: owns tab/session lifecycle and implements the `Page` protocol over it."""

from fastbrowse.browser.page import CdpPage
from fastbrowse.browser.session import BrowserSession

__all__ = ["BrowserSession", "CdpPage"]
