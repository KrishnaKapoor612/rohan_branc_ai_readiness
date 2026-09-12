#!/usr/bin/env python3
"""
robots.py -- robots.txt inspector for the crawl-render-audit skill.

Responsibilities:
    1. Retrieve robots.txt for the target origin.
    2. Determine whether the target URL is allowed by robots.txt.
    3. Preserve the matching Allow/Disallow rule as evidence.
    4. Extract Sitemap: declarations.
    5. Optionally report rules affecting known AI-related user agents.

This module does NOT:
    - crawl the target website;
    - fetch sitemap files;
    - render JavaScript;
    - bypass robots.txt;
    - assign finding severity;
    - emit final audit findings.

All operations are read-only and bounded.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = (
    "BrandAIReadinessAuditBot/1.0 "
    "(+https://github.com/KrishnaKapoor612/rohan_branc_ai_readiness)"
)

DEFAULT_TIMEOUT_SECONDS = 8.0

# Prevent an unexpectedly long redirect chain.
MAX_REDIRECTS = 5

# RFC 9309 recommends parsers support at least 500 KiB.
MAX_ROBOTS_BYTES = 500 * 1024

# Fixed observational list.
# These are NOT treated as an exhaustive list of AI crawlers.
AI_AGENT_TOKENS = [
    "GPTBot",
    "ChatGPT-User",
    "OAI-SearchBot",
    "ClaudeBot",
    "Claude-User",
    "Claude-SearchBot",
    "anthropic-ai",
    "Google-Extended",
    "Google-CloudVertexBot",
    "PerplexityBot",
    "Perplexity-User",
    "Bytespider",
    "CCBot",
    "Applebot-Extended",
    "Amazonbot",
    "Meta-ExternalAgent",
    "Meta-ExternalFetcher",
    "Diffbot",
    "cohere-ai",
    "AI2Bot",
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    """A single Allow or Disallow rule."""

    directive: str
    path: str
    line_number: int

    def evidence(self, user_agent_group: str) -> dict:
        return {
            "user_agent_group": user_agent_group,
            "directive": self.directive,
            "path": self.path,
            "line_number": self.line_number,
        }


@dataclass
class Group:
    """A robots.txt user-agent group."""

    user_agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)

    def label(self) -> str:
        return ", ".join(self.user_agents)


@dataclass
class ParsedRobots:
    groups: list[Group]
    sitemaps: list[str]
    ignored_sitemap_lines: list[dict]


@dataclass
class FetchResult:
    """
    Result of retrieving robots.txt.

    outcome:
        ok           -> robots.txt successfully retrieved.
        unavailable  -> 4xx response; no robots restriction was found.
        unknown      -> permission could not be verified.
    """

    outcome: str
    http_status: Optional[int]
    final_url: Optional[str]
    body: Optional[str]
    truncated: bool
    error: Optional[str]
    duration_ms: int


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

def normalize_url(raw_url: str) -> str:
    """
    Normalize the supplied target URL.

    If no scheme is supplied, HTTPS is assumed.

    No HTTP->HTTPS fallback is attempted automatically because that would
    introduce another origin/request and could change the audit target.
    """
    if not isinstance(raw_url, str):
        raise ValueError("URL must be a string.")

    raw_url = raw_url.strip()

    if not raw_url:
        raise ValueError("URL cannot be empty.")

    if "://" not in raw_url:
        raw_url = "https://" + raw_url

    parts = urlsplit(raw_url)

    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS URLs are supported.")

    if not parts.netloc:
        raise ValueError("URL does not contain a valid host.")

    # urlsplit() can raise on malformed ports.
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid port in URL: {exc}") from exc

    scheme = parts.scheme.lower()

    return urlunsplit(
        (
            scheme,
            parts.netloc,
            parts.path or "/",
            parts.query,
            "",
        )
    )


def robots_txt_url(target_url: str) -> str:
    """
    Construct robots.txt for the target origin.

    robots.txt belongs to the origin, so preserve:
        scheme
        hostname
        port

    but replace the path with /robots.txt.
    """
    parts = urlsplit(target_url)

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            "/robots.txt",
            "",
            "",
        )
    )


def target_path(target_url: str) -> str:
    """
    Return the URL path used for robots matching.

    Query parameters are intentionally excluded from the matching path.
    """
    path = urlsplit(target_url).path

    return path or "/"


# ---------------------------------------------------------------------------
# robots.txt retrieval
# ---------------------------------------------------------------------------

class RedirectLimiter(urllib.request.HTTPRedirectHandler):
    """HTTP redirect handler with an explicit maximum."""

    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum
        self.count = 0

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        self.count += 1

        if self.count > self.maximum:
            raise urllib.error.URLError(
                f"maximum redirect count ({self.maximum}) exceeded"
            )

        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )


def fetch_robots_txt(
    robots_url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> FetchResult:
    """
    Retrieve robots.txt once, with bounded redirects and response size.

    Interpretation:

        2xx -> robots.txt available; parse it.

        4xx -> robots.txt unavailable. Under RFC 9309 semantics,
               no robots restrictions are available from that resource.

        5xx/network/TLS/timeout/redirect failure ->
               permission cannot be verified.

    The latter is represented as UNKNOWN rather than DISALLOWED so that
    an audit limitation is not incorrectly turned into a confirmed defect.
    """

    redirect_handler = RedirectLimiter(MAX_REDIRECTS)

    opener = urllib.request.build_opener(redirect_handler)

    request = urllib.request.Request(
        robots_url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/plain, */*;q=0.1",
        },
        method="GET",
    )

    start = time.monotonic()

    try:
        with opener.open(request, timeout=timeout) as response:

            status = response.getcode()
            final_url = response.geturl()

            raw = response.read(MAX_ROBOTS_BYTES + 1)

            truncated = len(raw) > MAX_ROBOTS_BYTES

            if truncated:
                raw = raw[:MAX_ROBOTS_BYTES]

            text = raw.decode(
                "utf-8-sig",
                errors="replace",
            )

            duration_ms = int(
                (time.monotonic() - start) * 1000
            )

            if 200 <= status < 300:
                return FetchResult(
                    outcome="ok",
                    http_status=status,
                    final_url=final_url,
                    body=text,
                    truncated=truncated,
                    error=None,
                    duration_ms=duration_ms,
                )

            if 400 <= status < 500:
                return FetchResult(
                    outcome="unavailable",
                    http_status=status,
                    final_url=final_url,
                    body=None,
                    truncated=False,
                    error=None,
                    duration_ms=duration_ms,
                )

            return FetchResult(
                outcome="unknown",
                http_status=status,
                final_url=final_url,
                body=None,
                truncated=False,
                error=f"Unexpected HTTP status {status}",
                duration_ms=duration_ms,
            )

    except urllib.error.HTTPError as exc:

        duration_ms = int(
            (time.monotonic() - start) * 1000
        )

        if 400 <= exc.code < 500:
            return FetchResult(
                outcome="unavailable",
                http_status=exc.code,
                final_url=None,
                body=None,
                truncated=False,
                error=None,
                duration_ms=duration_ms,
            )

        return FetchResult(
            outcome="unknown",
            http_status=exc.code,
            final_url=None,
            body=None,
            truncated=False,
            error=f"HTTP {exc.code}: {exc.reason}",
            duration_ms=duration_ms,
        )

    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:

        duration_ms = int(
            (time.monotonic() - start) * 1000
        )

        reason = getattr(exc, "reason", exc)

        return FetchResult(
            outcome="unknown",
            http_status=None,
            final_url=None,
            body=None,
            truncated=False,
            error=str(reason),
            duration_ms=duration_ms,
        )

    except Exception as exc:

        duration_ms = int(
            (time.monotonic() - start) * 1000
        )

        return FetchResult(
            outcome="unknown",
            http_status=None,
            final_url=None,
            body=None,
            truncated=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# robots.txt parsing
# ---------------------------------------------------------------------------

def _compile_pattern(path: str) -> re.Pattern:
    """
    Compile a robots path pattern.

    Supports:
        * -> wildcard
        $ -> end anchor

    Matching is performed from the beginning of the path.
    """

    end_anchored = path.endswith("$")

    if end_anchored:
        path = path[:-1]

    pieces = path.split("*")

    regex_body = ".*".join(
        re.escape(piece)
        for piece in pieces
    )

    pattern = "^" + regex_body

    if end_anchored:
        pattern += "$"

    return re.compile(
        pattern,
        flags=re.IGNORECASE,
    )


def parse_robots_txt(text: str) -> ParsedRobots:
    """
    Parse User-agent, Allow, Disallow and Sitemap directives.

    Unknown directives are ignored.

    Empty Disallow values mean that nothing is disallowed and therefore
    do not create a blocking rule.
    """

    groups: list[Group] = []
    sitemaps: list[str] = []
    ignored_sitemap_lines: list[dict] = []

    current_group: Optional[Group] = None
    accepting_group_agents = False

    for line_number, raw_line in enumerate(
        text.splitlines(),
        start=1,
    ):

        # Remove comments.
        line = raw_line.split("#", 1)[0].strip()

        if not line:
            continue

        if ":" not in line:
            continue

        field, value = line.split(":", 1)

        field = field.strip().lower()
        value = value.strip()

        # ---------------------------------------------------------------
        # Sitemap
        # ---------------------------------------------------------------

        if field == "sitemap":

            if not value:
                continue

            # Sitemap locations should be absolute HTTP(S) URLs.
            sitemap_parts = urlsplit(value)

            if sitemap_parts.scheme.lower() in {"http", "https"} and sitemap_parts.netloc:
                if value not in sitemaps:
                    sitemaps.append(value)
            else:
                ignored_sitemap_lines.append(
                    {
                        "line_number": line_number,
                        "value": value,
                        "reason": "Sitemap URL is not an absolute HTTP(S) URL.",
                    }
                )

            continue

        # ---------------------------------------------------------------
        # User-agent
        # ---------------------------------------------------------------

        if field == "user-agent":

            if not value:
                continue

            token = value.lower()

            if (
                current_group is not None
                and accepting_group_agents
            ):
                current_group.user_agents.append(token)

            else:
                current_group = Group(
                    user_agents=[token]
                )

                groups.append(current_group)

            accepting_group_agents = True
            continue

        # ---------------------------------------------------------------
        # Rules
        # ---------------------------------------------------------------

        if field in {"allow", "disallow"}:

            accepting_group_agents = False

            if current_group is None:
                continue

            # Empty Disallow means "nothing".
            if field == "disallow" and not value:
                continue

            rule = Rule(
                directive=(
                    "Allow"
                    if field == "allow"
                    else "Disallow"
                ),
                path=value or "/",
                line_number=line_number,
            )

            current_group.rules.append(rule)

            continue

        # Any other directive is outside our scope.

    return ParsedRobots(
        groups=groups,
        sitemaps=sitemaps,
        ignored_sitemap_lines=ignored_sitemap_lines,
    )


# ---------------------------------------------------------------------------
# User-agent group selection
# ---------------------------------------------------------------------------

def select_group(
    groups: list[Group],
    user_agent: str,
) -> Optional[Group]:
    """
    Select the most specific applicable robots group.

    Specific user-agent groups take precedence over the wildcard group.
    Matching is case-insensitive.
    """

    agent = user_agent.lower()

    best_group: Optional[Group] = None
    best_length = -1

    wildcard_group: Optional[Group] = None

    for group in groups:

        for token in group.user_agents:

            if token == "*":
                wildcard_group = group
                continue

            if token and token in agent:

                if len(token) > best_length:
                    best_group = group
                    best_length = len(token)

    if best_group is not None:
        return best_group

    return wildcard_group


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------

def evaluate_path(
    group: Optional[Group],
    path: str,
) -> tuple[bool, Optional[Rule]]:
    """
    Evaluate the target path against one robots group.

    Rules are selected by longest matching path pattern.

    If equally specific Allow and Disallow rules both match,
    Allow wins.

    No matching rule means allowed.
    """

    if group is None:
        return True, None

    matches: list[tuple[int, Rule]] = []

    for rule in group.rules:

        regex = _compile_pattern(rule.path)

        if regex.match(path):
            matches.append(
                (
                    len(rule.path),
                    rule,
                )
            )

    if not matches:
        return True, None

    # Find maximum specificity.
    maximum_length = max(
        length
        for length, _ in matches
    )

    best_rules = [
        rule
        for length, rule in matches
        if length == maximum_length
    ]

    # Least restrictive rule wins ties.
    for rule in best_rules:

        if rule.directive == "Allow":
            return True, rule

    return False, best_rules[0]


# ---------------------------------------------------------------------------
# AI-agent inspection
# ---------------------------------------------------------------------------

def inspect_ai_agents(
    parsed: ParsedRobots,
    path: str,
    ai_agents: list[str],
) -> list[dict]:
    """
    Record only meaningful AI-agent robots observations.

    This function does NOT classify severity or create findings.
    """

    results = []

    for agent in ai_agents:

        group = select_group(
            parsed.groups,
            agent,
        )

        if group is None:
            continue

        allowed, rule = evaluate_path(
            group,
            path,
        )

        # Only report an agent when:
        #   1. a specific group mentions that agent, OR
        #   2. the selected rule actually disallows it.
        specific_group = any(
            token != "*"
            and token in agent.lower()
            for token in group.user_agents
        )

        if not specific_group and allowed:
            continue

        results.append(
            {
                "agent": agent,
                "crawl_allowed": allowed,
                "matched_rule": (
                    rule.evidence(group.label())
                    if rule
                    else None
                ),
            }
        )

    return results


# ---------------------------------------------------------------------------
# Main inspection
# ---------------------------------------------------------------------------

def check_robots(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ai_agents: Optional[list[str]] = None,
) -> dict:

    if ai_agents is None:
        ai_agents = AI_AGENT_TOKENS

    # ---------------------------------------------------------------
    # Normalize target
    # ---------------------------------------------------------------

    try:
        target_url = normalize_url(url)

    except Exception as exc:

        return {
            "skill": "crawl-render-audit",
            "check": "robots",
            "status": "error",
            "input_url": url,
            "error": str(exc),
        }

    robots_url = robots_txt_url(target_url)

    path = target_path(target_url)

    # ---------------------------------------------------------------
    # Fetch
    # ---------------------------------------------------------------

    fetch = fetch_robots_txt(
        robots_url=robots_url,
        user_agent=user_agent,
        timeout=timeout,
    )

    limitations: list[str] = []
    observations: list[str] = []

    # ---------------------------------------------------------------
    # Successful robots.txt retrieval
    # ---------------------------------------------------------------

    if fetch.outcome == "ok":

        parsed = parse_robots_txt(
            fetch.body or ""
        )

        if fetch.truncated:

            limitations.append(
                f"robots.txt exceeded {MAX_ROBOTS_BYTES} bytes; "
                "only the leading portion was inspected."
            )

        group = select_group(
            parsed.groups,
            user_agent,
        )

        crawl_allowed, matched_rule = evaluate_path(
            group,
            path,
        )

        if matched_rule is not None:

            matched_rule_evidence = (
                matched_rule.evidence(
                    group.label()
                )
                if group
                else None
            )

        else:
            matched_rule_evidence = None

        ai_directives = inspect_ai_agents(
            parsed,
            path,
            ai_agents,
        )

        if crawl_allowed:

            observations.append(
                "The target URL is allowed by the applicable "
                "robots.txt rules."
            )

        else:

            observations.append(
                "The target URL is disallowed by the applicable "
                "robots.txt rules."
            )

        return {
            "skill": "crawl-render-audit",
            "check": "robots",
            "status": "success",
            "input_url": target_url,
            "effective_user_agent": user_agent,
            "robots_url": robots_url,
            "robots_http_status": fetch.http_status,
            "robots_retrieved": True,
            "robots_final_url": fetch.final_url,
            "fetch_duration_ms": fetch.duration_ms,
            "crawl_permission": (
                "allowed"
                if crawl_allowed
                else "disallowed"
            ),
            "matched_rule": matched_rule_evidence,
            "sitemaps": parsed.sitemaps,
            "ignored_sitemap_lines": parsed.ignored_sitemap_lines,
            "ai_agent_directives": ai_directives,
            "observations": observations,
            "limitations": limitations,
        }

    # ---------------------------------------------------------------
    # robots.txt unavailable (4xx)
    # ---------------------------------------------------------------

    if fetch.outcome == "unavailable":

        observations.append(
            f"robots.txt returned HTTP {fetch.http_status}; "
            "no robots exclusion rules were available from that resource."
        )

        limitations.append(
            "robots.txt could not be inspected because the resource "
            f"returned HTTP {fetch.http_status}."
        )

        return {
            "skill": "crawl-render-audit",
            "check": "robots",
            "status": "success",
            "input_url": target_url,
            "effective_user_agent": user_agent,
            "robots_url": robots_url,
            "robots_http_status": fetch.http_status,
            "robots_retrieved": False,
            "robots_final_url": fetch.final_url,
            "fetch_duration_ms": fetch.duration_ms,
            "crawl_permission": "allowed",
            "matched_rule": None,
            "sitemaps": [],
            "ignored_sitemap_lines": [],
            "ai_agent_directives": [],
            "observations": observations,
            "limitations": limitations,
        }

    # ---------------------------------------------------------------
    # robots.txt could not be reached
    # ---------------------------------------------------------------

    error_detail = (
        f" ({fetch.error})"
        if fetch.error
        else ""
    )

    limitations.append(
        "robots.txt could not be retrieved"
        f"{error_detail}; crawl permission could not be verified."
    )

    observations.append(
        "Further crawling should not proceed until robots.txt "
        "permission can be verified."
    )

    return {
        "skill": "crawl-render-audit",
        "check": "robots",
        "status": "partial_failure",
        "input_url": target_url,
        "effective_user_agent": user_agent,
        "robots_url": robots_url,
        "robots_http_status": fetch.http_status,
        "robots_retrieved": False,
        "robots_final_url": fetch.final_url,
        "fetch_duration_ms": fetch.duration_ms,
        "crawl_permission": "unknown",
        "matched_rule": None,
        "sitemaps": [],
        "ignored_sitemap_lines": [],
        "ai_agent_directives": [],
        "observations": observations,
        "limitations": limitations,
        "error": fetch.error,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Inspect robots.txt permissions and sitemap declarations "
            "for a target URL."
        )
    )

    parser.add_argument(
        "url",
        help="Target URL or bare domain.",
    )

    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-agent used for robots.txt permission evaluation.",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="robots.txt request timeout in seconds.",
    )

    args = parser.parse_args()

    result = check_robots(
        url=args.url,
        user_agent=args.user_agent,
        timeout=args.timeout,
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )

    return (
        0
        if result.get("status") in {"success", "partial_failure"}
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())