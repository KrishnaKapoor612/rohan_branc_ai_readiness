#!/usr/bin/env python3
"""
render.py -- optional client-side rendering for the crawl-render-audit skill.

The marketplace ships no browser. Bundling one would break the submission size
limit and the runtime budget, and would make the package non-portable. So this
module is deliberately a capability probe first and a renderer second:

    - if a headless browser is available in the host environment, it renders;
    - if not, it says so plainly and returns available: False.

It never fabricates a rendered document, and it never lets its own absence be
read as evidence about the site. check.py degrades to inferring client-side
dependence from hydration markers and drops that finding to medium confidence,
which is the honest answer when rendering could not be observed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from typing import Optional

DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_HTML_BYTES = 4 * 1024 * 1024


def _probe_backend() -> tuple[Optional[str], Optional[str]]:
    """Return (backend_name, unavailable_reason)."""
    if importlib.util.find_spec("playwright") is not None:
        return "playwright", None
    if importlib.util.find_spec("selenium") is not None:
        return "selenium", None
    return None, (
        "No headless browser binding is installed in this environment "
        "(checked playwright and selenium). Rendered content could not be "
        "verified; it was not assumed either way."
    )


def _render_playwright(url: str, timeout: float) -> dict:
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=int(timeout * 1000))
            html = page.content()[:MAX_HTML_BYTES]
            text = page.inner_text("body")[:MAX_HTML_BYTES]
        finally:
            browser.close()

    return {
        "available": True,
        "backend": "playwright",
        "outcome": "rendered",
        "html": html,
        "text": text,
        "error": None,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def render_page(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """
    Render one URL if a browser exists. Read-only: navigation only, no clicks,
    no form submission, no authentication, no consent dismissal.
    """
    backend, reason = _probe_backend()

    if backend is None:
        return {
            "available": False,
            "backend": None,
            "outcome": "unavailable",
            "html": None,
            "text": None,
            "error": reason,
            "duration_ms": 0,
        }

    started = time.monotonic()
    try:
        if backend == "playwright":
            return _render_playwright(url, timeout)
        return {
            "available": False,
            "backend": backend,
            "outcome": "unavailable",
            "html": None,
            "text": None,
            "error": (
                "Only the playwright backend is implemented. A selenium binding was "
                "detected but is not driven by this module."
            ),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "available": True,
            "backend": backend,
            "outcome": "error",
            "html": None,
            "text": None,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a URL if a headless browser is available."
    )
    parser.add_argument("url")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--no-body", action="store_true")

    args = parser.parse_args()
    result = render_page(args.url, timeout=args.timeout)

    if args.no_body:
        result = {k: v for k, v in result.items() if k not in ("html", "text")}

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
