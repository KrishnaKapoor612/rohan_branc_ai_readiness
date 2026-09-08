#!/usr/bin/env python3
"""
fetch.py -- bounded, read-only HTTP retrieval for the crawl-render-audit skill.

Responsibilities:
    1. Retrieve a single URL with bounded bytes, redirects and time.
    2. Report exactly what happened, without interpreting it.
    3. Probe whether the origin serves different responses to different
       user agents (the stated-policy versus actual-behaviour gap).

This module does NOT:
    - decide whether anything is a defect;
    - retry aggressively or crawl;
    - disable TLS verification;
    - send cookies, credentials or any non-GET method.

Every function returns data. Interpretation belongs to check.py.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_USER_AGENT = (
    "BrandAIReadinessAuditBot/1.0 "
    "(+https://github.com/KrishnaKapoor612/brand-ai-readiness-audit)"
)

# A mainstream browser string and a well-known assistant crawler string.
# Used only to compare responses, never to impersonate for access.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ASSISTANT_USER_AGENT = "GPTBot/1.1 (+https://openai.com/gptbot)"

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024
POLITE_DELAY_SECONDS = 1.0


class RedirectLimiter(urllib.request.HTTPRedirectHandler):
    """Bounds the redirect chain and records every hop as evidence."""

    def __init__(self, maximum: int = MAX_REDIRECTS) -> None:
        self.maximum = maximum
        self.count = 0
        self.chain: list[dict] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        self.chain.append({
            "from": req.full_url,
            "to": newurl,
            "status": code,
        })
        if self.count > self.maximum:
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"Exceeded {self.maximum} redirects", headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class FetchResult:
    outcome: str  # retrieved | unavailable | error
    http_status: Optional[int] = None
    final_url: Optional[str] = None
    redirect_chain: list = field(default_factory=list)
    content_type: Optional[str] = None
    content_encoding: Optional[str] = None
    body: Optional[str] = None
    bytes: int = 0
    truncated: bool = False
    error: Optional[str] = None
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "http_status": self.http_status,
            "final_url": self.final_url,
            "redirect_chain": self.redirect_chain,
            "content_type": self.content_type,
            "content_encoding": self.content_encoding,
            "body": self.body,
            "bytes": self.bytes,
            "truncated": self.truncated,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


def _decompress(raw: bytes, encoding: Optional[str]) -> bytes:
    if not encoding:
        return raw
    encoding = encoding.lower()
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        return raw
    return raw


def _charset(content_type: Optional[str]) -> str:
    if content_type and "charset=" in content_type.lower():
        return content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
    return "utf-8"


def fetch_page(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_BODY_BYTES,
    accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
) -> dict:
    """
    Retrieve one URL. GET only, TLS verified, bounded in bytes and redirects.

    outcome is one of:
        retrieved   - a 2xx response with a usable body
        unavailable - the server answered with a non-2xx status
        error       - DNS, TLS, timeout or transport failure

    A non-2xx status is a fact about this request from this environment. It is
    never, on its own, a statement about the site's availability to others.
    """
    started = time.monotonic()

    limiter = RedirectLimiter()
    opener = urllib.request.build_opener(limiter)

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": user_agent,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en",
        },
    )

    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            raw = raw[:max_bytes]

            content_type = response.headers.get("Content-Type")
            content_encoding = response.headers.get("Content-Encoding")
            decoded = _decompress(raw, content_encoding)

            body = decoded.decode(_charset(content_type), errors="replace")

            return FetchResult(
                outcome="retrieved",
                http_status=response.status,
                final_url=response.url,
                redirect_chain=limiter.chain,
                content_type=content_type,
                content_encoding=content_encoding,
                body=body,
                bytes=len(decoded),
                truncated=truncated,
                duration_ms=int((time.monotonic() - started) * 1000),
            ).as_dict()

    except urllib.error.HTTPError as exc:
        body = None
        size = 0
        try:
            raw = exc.read(max_bytes)
            size = len(raw)
            body = raw.decode("utf-8", errors="replace")
        except Exception:
            pass

        return FetchResult(
            outcome="unavailable",
            http_status=exc.code,
            final_url=exc.url if hasattr(exc, "url") else url,
            redirect_chain=limiter.chain,
            content_type=exc.headers.get("Content-Type") if exc.headers else None,
            body=body,
            bytes=size,
            error=f"HTTP {exc.code} {exc.reason}",
            duration_ms=int((time.monotonic() - started) * 1000),
        ).as_dict()

    except ssl.SSLError as exc:
        return FetchResult(
            outcome="error",
            final_url=url,
            redirect_chain=limiter.chain,
            error=f"TLS failure: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        ).as_dict()

    except socket.timeout:
        return FetchResult(
            outcome="error",
            final_url=url,
            redirect_chain=limiter.chain,
            error=f"Request timed out after {timeout}s.",
            duration_ms=int((time.monotonic() - started) * 1000),
        ).as_dict()

    except Exception as exc:
        return FetchResult(
            outcome="error",
            final_url=url,
            redirect_chain=limiter.chain,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        ).as_dict()


def probe_user_agents(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    delay: float = POLITE_DELAY_SECONDS,
) -> dict:
    """
    Request the same URL three times with different user agents and compare.

    robots.txt states a policy. This measures behaviour. A site may permit an
    assistant crawler in robots.txt and still have an edge rule that answers it
    with 403 or a challenge page, in which case the stated permission is
    meaningless. The two facts have to be measured separately.

    Only three requests, spaced by a polite delay. No impersonation is used to
    obtain content that would otherwise be refused; the responses are compared
    and then discarded.
    """
    agents = [
        ("browser", BROWSER_USER_AGENT),
        ("audit_bot", DEFAULT_USER_AGENT),
        ("assistant_crawler", ASSISTANT_USER_AGENT),
    ]

    probes = []

    for index, (label, agent) in enumerate(agents):
        if index:
            time.sleep(delay)

        result = fetch_page(url, user_agent=agent, timeout=timeout)
        body = result.get("body") or ""

        probes.append({
            "label": label,
            "user_agent": agent,
            "outcome": result["outcome"],
            "http_status": result["http_status"],
            "bytes": result["bytes"],
            "text_sha256_12": hashlib.sha256(body.encode("utf-8")).hexdigest()[:12],
            "error": result["error"],
        })

    statuses = {p["label"]: p["http_status"] for p in probes}
    browser_ok = statuses.get("browser") == 200

    divergent = sorted(
        p["label"] for p in probes
        if browser_ok and p["label"] != "browser" and p["http_status"] != 200
    )

    # Same status but a materially smaller body also indicates differentiation.
    browser_bytes = next(
        (p["bytes"] for p in probes if p["label"] == "browser"), 0
    )
    thin = sorted(
        p["label"] for p in probes
        if browser_ok
        and p["label"] != "browser"
        and p["http_status"] == 200
        and browser_bytes > 0
        and p["bytes"] < browser_bytes * 0.5
    )

    return {
        "url": url,
        "probes": probes,
        "browser_baseline_ok": browser_ok,
        "blocked_labels": divergent,
        "reduced_content_labels": thin,
        "differentiates_by_user_agent": bool(divergent or thin),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded read-only retrieval and user-agent differential probe."
    )
    parser.add_argument("url")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-bytes", type=int, default=MAX_BODY_BYTES)
    parser.add_argument(
        "--probe-user-agents",
        action="store_true",
        help="Compare responses across browser, audit and assistant user agents.",
    )
    parser.add_argument(
        "--no-body",
        action="store_true",
        help="Omit the response body from stdout.",
    )

    args = parser.parse_args()

    if args.probe_user_agents:
        print(json.dumps(probe_user_agents(args.url, timeout=args.timeout), indent=2))
        return 0

    result = fetch_page(
        args.url,
        user_agent=args.user_agent,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
    )

    if args.no_body:
        result = {k: v for k, v in result.items() if k != "body"}

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())