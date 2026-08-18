"""Scrape.do transport providers used by marketplace services.

The token is deliberately read from Django settings and is never included in
the returned result or log messages.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderResult:
    success: bool
    provider: str
    status_code: int | None = None
    data: dict[str, Any] | None = None
    html: str = ""
    error: str | None = None
    request_cost: str | None = None
    duration_ms: int | None = None

    def as_dict(self):
        return {
            "success": self.success,
            "provider": self.provider,
            "data": self.data,
            "html": self.html,
            "status_code": self.status_code,
            "error": self.error,
            "request_cost": self.request_cost,
            "duration_ms": self.duration_ms,
        }


class ScrapeDoError(RuntimeError):
    def __init__(self, message, *, status_code=None, provider="scrapedo",
                 retryable=False, operation="request", duration_ms=None,
                 request_cost=None):
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider
        self.retryable = retryable
        self.operation = operation
        self.duration_ms = duration_ms
        self.request_cost = request_cost


class ScrapeDoWebProvider:
    endpoint = "https://api.scrape.do/"
    name = "scrapedo_web"

    def fetch(self, target_url: str, **options) -> ProviderResult:
        token = getattr(settings, "SCRAPEDO_API_TOKEN", "").strip()
        if not token:
            raise ScrapeDoError("SCRAPEDO_API_TOKEN is not configured", provider=self.name)
        params = {"token": token, "url": target_url}
        params.update({key: value for key, value in options.items() if value is not None})
        started = time.monotonic()
        try:
            response = requests.get(
                self.endpoint,
                params=params,
                timeout=getattr(settings, "SCRAPEDO_TIMEOUT", 45),
            )
        except requests.Timeout as exc:
            duration_ms = _duration_ms(started)
            logger.error("[SCRAPEDO] provider=%s operation=web_fetch status=timeout request_cost=unknown duration_ms=%s error_type=timeout", self.name, duration_ms)
            raise ScrapeDoError("Scrape.do request timed out", provider=self.name, retryable=True, duration_ms=duration_ms, operation="web_fetch") from exc
        except requests.RequestException as exc:
            duration_ms = _duration_ms(started)
            logger.error("[SCRAPEDO] provider=%s operation=web_fetch status=error request_cost=unknown duration_ms=%s error_type=%s", self.name, exc.__class__.__name__, duration_ms)
            raise ScrapeDoError("Scrape.do request failed", provider=self.name, retryable=True, duration_ms=duration_ms, operation="web_fetch") from exc
        cost = response.headers.get("Scrape.do-Request-Cost")
        duration_ms = _duration_ms(started)
        logger.info("[SCRAPEDO] provider=%s operation=web_fetch status=%s request_cost=%s duration_ms=%s", self.name, response.status_code, cost or "unknown", duration_ms)
        if not response.ok:
            logger.error("[SCRAPEDO] provider=%s operation=web_fetch status=%s request_cost=%s duration_ms=%s error_type=http", self.name, response.status_code, cost or "unknown", duration_ms)
            raise ScrapeDoError("Scrape.do returned an HTTP error", status_code=response.status_code, provider=self.name, retryable=response.status_code == 429 or response.status_code >= 500, duration_ms=duration_ms, request_cost=cost, operation="web_fetch")
        return ProviderResult(True, self.name, response.status_code, html=response.text, request_cost=cost, duration_ms=duration_ms)


class ScrapeDoAmazonProvider:
    endpoint = "https://api.scrape.do/plugin/amazon"
    name = "scrapedo_amazon"

    def _request(self, action: str, **params) -> ProviderResult:
        token = getattr(settings, "SCRAPEDO_API_TOKEN", "").strip()
        if not token:
            raise ScrapeDoError("SCRAPEDO_API_TOKEN is not configured", provider=self.name)
        query = {"token": token, **params}
        url = f"{self.endpoint}/{action}"
        started = time.monotonic()
        try:
            response = requests.get(url, params=query, timeout=getattr(settings, "SCRAPEDO_TIMEOUT", 45))
        except requests.Timeout as exc:
            duration_ms = _duration_ms(started)
            logger.error("[SCRAPEDO] provider=%s operation=%s status=timeout request_cost=unknown duration_ms=%s error_type=timeout", self.name, action, duration_ms)
            raise ScrapeDoError("Scrape.do Amazon request timed out", provider=self.name, retryable=True, duration_ms=duration_ms, operation=action) from exc
        except requests.RequestException as exc:
            duration_ms = _duration_ms(started)
            logger.error("[SCRAPEDO] provider=%s operation=%s status=error request_cost=unknown duration_ms=%s error_type=%s", self.name, action, duration_ms, exc.__class__.__name__)
            raise ScrapeDoError("Scrape.do Amazon request failed", provider=self.name, retryable=True, duration_ms=duration_ms, operation=action) from exc
        cost = response.headers.get("Scrape.do-Request-Cost")
        duration_ms = _duration_ms(started)
        logger.info("[SCRAPEDO] provider=%s operation=%s status=%s request_cost=%s duration_ms=%s", self.name, action, response.status_code, cost or "unknown", duration_ms)
        if not response.ok:
            logger.error("[SCRAPEDO] provider=%s operation=%s status=%s request_cost=%s duration_ms=%s error_type=http", self.name, action, response.status_code, cost or "unknown", duration_ms)
            raise ScrapeDoError("Scrape.do Amazon returned an HTTP error", status_code=response.status_code, provider=self.name, retryable=response.status_code == 429 or response.status_code >= 500, duration_ms=duration_ms, request_cost=cost, operation=action)
        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("[SCRAPEDO] provider=%s operation=%s status=%s request_cost=%s duration_ms=%s error_type=invalid_json", self.name, action, response.status_code, cost or "unknown", duration_ms)
            raise ScrapeDoError("Scrape.do Amazon returned invalid JSON", status_code=response.status_code, provider=self.name, duration_ms=duration_ms, request_cost=cost, operation=action) from exc
        if not isinstance(payload, dict) or str(payload.get("status", "success")).lower() == "error":
            logger.error("[SCRAPEDO] provider=%s operation=%s status=%s request_cost=%s duration_ms=%s error_type=unsuccessful_response", self.name, action, response.status_code, cost or "unknown", duration_ms)
            raise ScrapeDoError("Scrape.do Amazon returned an unsuccessful response", status_code=response.status_code, provider=self.name, duration_ms=duration_ms, request_cost=cost, operation=action)
        return ProviderResult(True, self.name, response.status_code, data=payload, request_cost=cost, duration_ms=duration_ms)

    def search(self, keyword: str, page: int = 1) -> ProviderResult:
        return self._request("search", keyword=keyword, geocode=getattr(settings, "AMAZON_MARKETPLACE_GEOCODE", "in"), page=page)

    def product(self, asin: str) -> ProviderResult:
        return self._request("pdp", asin=asin, geocode=getattr(settings, "AMAZON_MARKETPLACE_GEOCODE", "in"))


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
