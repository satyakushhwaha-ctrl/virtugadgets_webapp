"""Shared Playwright runtime settings."""

import os


def is_headless() -> bool:
    """Use headless Chromium by default for Linux/Railway compatibility."""
    value = os.getenv("PLAYWRIGHT_HEADLESS", "true")
    return value.strip().lower() not in {"0", "false", "no", "off"}
