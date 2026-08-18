"""Cheap, credential-safe Scrape.do diagnostics.

The checks use HEAD requests only.  They validate configuration and network
reachability without fetching an Amazon product or consuming a product-page
request.  The token is sent to the provider but is never returned or logged.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeDoHealth:
    token_configured: bool
    generic_reachable: bool
    amazon_reachable: bool
    generic_status: int | None = None
    amazon_status: int | None = None
    error: str | None = None

    @property
    def healthy(self):
        return self.token_configured and self.generic_reachable and self.amazon_reachable

    def as_dict(self):
        return {**asdict(self), "healthy": self.healthy}


def check_scrapedo_health() -> ScrapeDoHealth:
    token = getattr(settings, "SCRAPEDO_API_TOKEN", "").strip()
    if not token:
        return ScrapeDoHealth(False, False, False, error="SCRAPEDO_API_TOKEN is not configured")

    timeout = getattr(settings, "SCRAPEDO_TIMEOUT", 45)
    checks = (
        ("generic", "https://api.scrape.do/"),
        ("amazon", "https://api.scrape.do/plugin/amazon/search"),
    )
    results = {}
    errors = []
    for name, endpoint in checks:
        try:
            response = requests.head(
                endpoint,
                params={"token": token},
                timeout=timeout,
                allow_redirects=True,
            )
            results[name] = (response.status_code < 500, response.status_code)
        except requests.RequestException as exc:
            results[name] = (False, None)
            errors.append(f"{name}:{exc.__class__.__name__}")
            logger.warning("[SCRAPEDO HEALTH] endpoint=%s error_type=%s", name, exc.__class__.__name__)

    return ScrapeDoHealth(
        token_configured=True,
        generic_reachable=results["generic"][0],
        amazon_reachable=results["amazon"][0],
        generic_status=results["generic"][1],
        amazon_status=results["amazon"][1],
        error="; ".join(errors) or None,
    )


def validate_scrapedo_configuration(*, strict=False):
    """Validate settings without ever including the credential in output."""
    configured = bool(getattr(settings, "SCRAPEDO_API_TOKEN", "").strip())
    if strict and not configured:
        raise RuntimeError("SCRAPEDO_API_TOKEN must be configured for Scrape.do.")
    return {"token_configured": configured, "timeout": getattr(settings, "SCRAPEDO_TIMEOUT", 45)}
