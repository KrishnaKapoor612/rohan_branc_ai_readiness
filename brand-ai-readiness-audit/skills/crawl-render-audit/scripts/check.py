#!/usr/bin/env python3
"""
check.py -- specialist coordinator for the crawl-render-audit skill.

Implements the marketplace specialist contract
(see references/specialist-contract.md).

Responsibilities:
    1. Run the robots.txt permission check before anything else.
    2. Retrieve the target only where permission allows it.
    3. Analyse what a non-executing machine reader can actually extract.
    4. Compare server HTML against rendered content where a renderer exists.
    5. Emit one specialist result object: observations, findings, limitations.

This module does NOT:
    - assign severity labels (it emits severity_inputs; the orchestrator scores);
    - assign global finding IDs;
    - bypass any access control;
    - modify the target site in any way;
    - treat an unavailable check as either a defect or a pass.

Sibling modules (robots.py, fetch.py, render.py, sitemap.py) are optional at run
time. A missing sibling becomes a limitation, never a crash and never a finding.

All operations are read-only and bounded.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from typing import Any, Optional

SKILL_ID = "crawl-render-audit"

DEFAULT_USER_AGENT = (
    "BrandAIReadinessAuditBot/1.0 "
    "(+https://github.com/KrishnaKapoor612/brand-ai-readiness-audit)"
)

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_BUDGET_MS = 60_000

# Body text below this many characters in the server response, on a page that
# ships a hydration payload, indicates the page is assembled client-side.
THIN_TEXT_CHARS = 600

# Markers that a page's content is produced by client-side execution.
HYDRATION_MARKERS = (
    "__NEXT_DATA__",
    "__NUXT__",
    "__INITIAL_STATE__",
    "__APOLLO_STATE__",
    "window.__data",
    "ng-version",
    "data-reactroot",
)


# ---------------------------------------------------------------------------
# Sibling module loading
# ---------------------------------------------------------------------------

def load_sibling(module_name: str) -> tuple[Optional[Any], Optional[str]]:
    """
    Import a sibling script from this skill's scripts/ directory.

    Returns (module, None) on success and (None, reason) on failure. The reason
    is carried into the audit limitation so that a genuine import error is never
    mistaken for a module that was simply not written yet.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, f"{module_name}.py")

    if not os.path.isfile(path):
        return None, f"{module_name}.py is not present at {path}."

    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None, f"{module_name}.py could not be loaded as a module."
        module = importlib.util.module_from_spec(spec)
        # Required before exec_module: modules that combine `from __future__
        # import annotations` with @dataclass resolve their annotations through
        # sys.modules[cls.__module__], which raises if the module is absent.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module, None
    except Exception as exc:
        return None, f"{module_name}.py failed to import: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Minimal HTML text extraction (stdlib only)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collects visible text, ignoring script and style content."""

    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._depth > 0:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth == 0:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)

    def text(self) -> str:
        return " ".join(self.chunks)


def visible_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # Malformed markup: fall back to a crude strip rather than failing the run.
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def analyze_html(html: str) -> dict:
    """
    Describe what a non-executing reader can extract from a server response.

    Returns observations only. Interpretation happens in the caller, so that
    this function stays reusable and testable without network access.
    """
    text = visible_text(html)

    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL
    )
    title = title_match.group(1).strip() if title_match else None

    desc_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    description = desc_match.group(1).strip() if desc_match else None

    ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    )

    ld_valid = 0
    ld_invalid = 0
    ld_types: list[str] = []

    for block in ld_blocks:
        try:
            parsed = json.loads(block.strip())
        except Exception:
            ld_invalid += 1
            continue
        ld_valid += 1
        for node in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(node, dict):
                node_type = node.get("@type")
                if isinstance(node_type, str):
                    ld_types.append(node_type)
                elif isinstance(node_type, list):
                    ld_types.extend(t for t in node_type if isinstance(t, str))

    canonical = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](.*?)["\']',
        html,
        re.IGNORECASE,
    )

    images = re.findall(r"<img\b[^>]*>", html, re.IGNORECASE)
    images_without_alt = [
        tag for tag in images
        if not re.search(r'\balt\s*=\s*["\'][^"\']+["\']', tag, re.IGNORECASE)
    ]

    markers = [m for m in HYDRATION_MARKERS if m in html]

    return {
        "title": title,
        "meta_description": description,
        "text_chars": len(text),
        "h1_count": len(re.findall(r"<h1\b", html, re.IGNORECASE)),
        "h2_count": len(re.findall(r"<h2\b", html, re.IGNORECASE)),
        "jsonld_blocks": len(ld_blocks),
        "jsonld_parseable": ld_valid,
        "jsonld_unparseable": ld_invalid,
        "jsonld_types": sorted(set(ld_types)),
        "canonical": canonical.group(1) if canonical else None,
        "image_count": len(images),
        "images_without_alt": len(images_without_alt),
        "hydration_markers": markers,
        "html_bytes": len(html),
    }


# ---------------------------------------------------------------------------
# Finding construction
# ---------------------------------------------------------------------------

def finding(
    local_id: str,
    title: str,
    category: str,
    stage: str,
    evidence: list[str],
    stage_block: int,
    fact_criticality: int,
    blast_radius: int,
    scope: str,
    affected_urls: list[str],
    action_summary: str,
    action_rationale: str,
    effort: str,
    verify_by: str,
    confidence: str = "high",
) -> dict:
    """Build a contract-shaped finding. Severity is deliberately absent."""
    return {
        "local_id": local_id,
        "title": title,
        "category": category,
        "stage": stage,
        "confidence": confidence,
        "scope": scope,
        "affected_urls": sorted(affected_urls),
        "evidence": evidence,
        "severity_inputs": {
            "stage_block": stage_block,
            "fact_criticality": fact_criticality,
            "blast_radius": blast_radius,
        },
        "suggested_action": {
            "summary": action_summary,
            "rationale": action_rationale,
            "effort": effort,
            "verify_by": verify_by,
        },
    }


def radius_from_rule_path(path: Optional[str]) -> tuple[int, str]:
    """A disallow on / is site-wide; a disallow on a subtree is a section."""
    if path in (None, "", "/"):
        return 3, "site_wide"
    return 2, "section"


# ---------------------------------------------------------------------------
# Stage 1 -- crawl permission
# ---------------------------------------------------------------------------

def stage_robots(url: str, user_agent: str, timeout: float, state: dict) -> Optional[dict]:
    robots, load_error = load_sibling("robots")

    if robots is None:
        state["limitations"].append({
            "check": "robots_policy",
            "reason": load_error or "robots.py is unavailable.",
            "affected_checks": ["crawl_permission", "ai_agent_directives", "sitemap_discovery"],
        })
        state["status"] = "partial_failure"
        return None

    result = robots.check_robots(url=url, user_agent=user_agent, timeout=timeout)

    state["observations"].extend(result.get("observations", []))
    for text in result.get("limitations", []):
        state["limitations"].append({
            "check": "robots_policy",
            "reason": text,
            "affected_checks": ["crawl_permission"],
        })

    permission = result.get("crawl_permission")
    robots_url = result.get("robots_url")

    # Finding: the audit's own user agent is disallowed on the target path.
    if permission == "disallowed":
        rule = result.get("matched_rule") or {}
        radius, scope = radius_from_rule_path(rule.get("path"))
        state["findings"].append(finding(
            local_id="CR-001",
            title="Target path is disallowed by robots.txt for general crawlers",
            category="crawl_policy",
            stage="reachable",
            evidence=[
                f"robots.txt at {robots_url} returned HTTP {result.get('robots_http_status')}.",
                f"Matched rule: {json.dumps(rule, sort_keys=True)}",
                f"Evaluated path: {result.get('input_url')}",
            ],
            stage_block=4,
            fact_criticality=3,
            blast_radius=radius,
            scope=scope,
            affected_urls=[result.get("input_url", url)],
            action_summary=(
                "Narrow the Disallow rule so that content intended for public "
                "discovery is crawlable, keeping only genuinely private paths blocked."
            ),
            action_rationale=(
                "A crawler that is refused entry never reaches the later stages of "
                "discovery. No amount of on-page quality can compensate, because the "
                "page is never read."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm crawl_permission resolves to allowed.",
        ))

    # Finding: assistant-operated crawlers are specifically excluded.
    blocked_agents = [
        entry for entry in result.get("ai_agent_directives", [])
        if entry.get("crawl_allowed") is False
    ]

    if blocked_agents:
        paths = {
            (entry.get("matched_rule") or {}).get("path")
            for entry in blocked_agents
        }
        radius, scope = (3, "site_wide") if paths & {None, "", "/"} else (2, "section")
        names = sorted(entry["agent"] for entry in blocked_agents)

        state["findings"].append(finding(
            local_id="CR-002",
            title="Crawlers operated by AI assistants are disallowed by robots.txt",
            category="crawl_policy",
            stage="reachable",
            evidence=[
                f"robots.txt at {robots_url} returned HTTP {result.get('robots_http_status')}.",
                f"Disallowed agent tokens: {', '.join(names)}.",
                f"Matching rules: {json.dumps(sorted((e.get('matched_rule') or {}).get('path') or '/' for e in blocked_agents))}",
            ],
            stage_block=4,
            fact_criticality=3,
            blast_radius=radius,
            scope=scope,
            affected_urls=[result.get("input_url", url)],
            action_summary=(
                "Allow the assistant crawler tokens on public marketing, product and "
                "support paths. Keep account, checkout and internal search paths blocked."
            ),
            action_rationale=(
                "Assistants build answers from pages they are permitted to fetch. A "
                "token-level disallow removes the brand from the candidate set before "
                "any ranking or quality judgement happens, which is the most complete "
                "form of invisibility available."
            ),
            effort="low",
            verify_by=(
                "Re-run this audit and confirm no assistant token resolves to "
                "crawl_allowed false on public paths."
            ),
        ))

    # Sitemap discovery is an observation, not a defect on its own.
    sitemaps = result.get("sitemaps", [])
    if sitemaps:
        state["observations"].append(
            f"robots.txt declares {len(sitemaps)} sitemap location(s)."
        )
    else:
        state["observations"].append(
            "robots.txt declares no Sitemap: location. Not a defect by itself; "
            "page reachability through crawlable links is checked separately."
        )

    return result


# ---------------------------------------------------------------------------
# Stage 2 -- retrieval
# ---------------------------------------------------------------------------

def stage_fetch(url: str, user_agent: str, timeout: float, state: dict) -> Optional[dict]:
    fetch, load_error = load_sibling("fetch")

    if fetch is None:
        state["limitations"].append({
            "check": "http_retrieval",
            "reason": load_error or "fetch.py is unavailable.",
            "affected_checks": [
                "server_html_extractability",
                "structured_data_presence",
                "render_diff",
            ],
        })
        state["status"] = "partial_failure"
        return None

    result = fetch.fetch_page(url, user_agent=user_agent, timeout=timeout)

    state["observations"].append(
        f"Retrieval of {url}: outcome={result.get('outcome')} "
        f"status={result.get('http_status')} "
        f"bytes={result.get('bytes')} "
        f"duration_ms={result.get('duration_ms')}"
    )

    if result.get("outcome") != "retrieved":
        state["limitations"].append({
            "check": "http_retrieval",
            "reason": (
                f"Target returned outcome={result.get('outcome')} "
                f"status={result.get('http_status')} "
                f"error={result.get('error')} from this audit environment. "
                "This does not establish that the site is unreachable for all clients."
            ),
            "affected_checks": ["server_html_extractability", "render_diff"],
        })
        state["status"] = "partial_failure"
        return None

    return result


# ---------------------------------------------------------------------------
# Stage 3 -- extractability of the server response
# ---------------------------------------------------------------------------

def stage_extractability(url: str, html: str, state: dict) -> dict:
    analysis = analyze_html(html)

    state["observations"].append(
        "Server HTML analysis: " + json.dumps(analysis, sort_keys=True)
    )

    # Client-side dependence, inferred without a renderer.
    if analysis["text_chars"] < THIN_TEXT_CHARS and analysis["hydration_markers"]:
        state["findings"].append(finding(
            local_id="CR-003",
            title="Page content is assembled client-side and is absent from the server response",
            category="rendering",
            stage="renderable",
            evidence=[
                f"{url} returned {analysis['html_bytes']} bytes of HTML "
                f"containing only {analysis['text_chars']} characters of extractable text.",
                f"Client-side hydration markers present: {', '.join(analysis['hydration_markers'])}.",
            ],
            stage_block=3,
            fact_criticality=3,
            blast_radius=1,
            scope="single_page",
            affected_urls=[url],
            action_summary=(
                "Server-render or pre-render the primary content of this page so the "
                "facts are present in the initial HTML response."
            ),
            action_rationale=(
                "Readers that do not execute JavaScript receive a shell. Any fact that "
                "only exists after hydration cannot be quoted, because it was never "
                "part of the document the reader received."
            ),
            effort="high",
            verify_by=(
                "Fetch the URL without JavaScript execution and confirm the key facts "
                "appear in the response body."
            ),
            confidence="medium",
        ))

    # Structured data.
    if analysis["jsonld_blocks"] == 0:
        state["findings"].append(finding(
            local_id="CR-004",
            title="No structured data present on the inspected page",
            category="structured_data",
            stage="extractable",
            evidence=[
                f"{url}: 0 application/ld+json blocks found in the server response.",
                f"Page carries a title ({bool(analysis['title'])}) and "
                f"{analysis['h1_count']} h1 element(s), so content exists but is unlabelled.",
            ],
            stage_block=2,
            fact_criticality=3,
            blast_radius=1,
            scope="single_page",
            affected_urls=[url],
            action_summary=(
                "Add schema.org JSON-LD describing what this page is about. Use "
                "Organization on the home page and Product with an Offer, including "
                "price, priceCurrency and availability, on product pages."
            ),
            action_rationale=(
                "Structured data states a fact unambiguously and in one place, which "
                "is what a machine extracts reliably. Prose requires inference; a "
                "labelled field does not."
            ),
            effort="medium",
            verify_by="Re-run this audit and confirm parseable JSON-LD is detected.",
        ))
    elif analysis["jsonld_unparseable"] > 0:
        state["findings"].append(finding(
            local_id="CR-005",
            title="Structured data is present but not parseable",
            category="structured_data",
            stage="extractable",
            evidence=[
                f"{url}: {analysis['jsonld_unparseable']} of {analysis['jsonld_blocks']} "
                "ld+json blocks failed JSON parsing.",
                f"Parseable types found: {', '.join(analysis['jsonld_types']) or 'none'}.",
            ],
            stage_block=2,
            fact_criticality=3,
            blast_radius=1,
            scope="single_page",
            affected_urls=[url],
            action_summary=(
                "Fix the malformed JSON-LD blocks. Validate output at build time so "
                "template changes cannot silently break the markup."
            ),
            action_rationale=(
                "Invalid markup is discarded silently by consumers. The effort of "
                "adding structured data is spent, but none of the benefit is received."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm jsonld_unparseable is 0.",
        ))

    # Document identity.
    if not analysis["title"]:
        state["findings"].append(finding(
            local_id="CR-006",
            title="Page has no title element",
            category="machine_readability",
            stage="extractable",
            evidence=[f"{url}: no non-empty <title> element in the server response."],
            stage_block=2,
            fact_criticality=3,
            blast_radius=1,
            scope="single_page",
            affected_urls=[url],
            action_summary=(
                "Give every page a unique title stating the entity and the page's "
                "subject, for example 'Velocity X9 Running Shoes | Garuda Footwear'."
            ),
            action_rationale=(
                "The title is the shortest unambiguous statement of what a document is "
                "about, and is weighted heavily when a system decides whether a page "
                "answers a question."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm a title is detected.",
        ))

    return analysis


# ---------------------------------------------------------------------------
# Stage 4 -- render comparison
# ---------------------------------------------------------------------------

def stage_render(url: str, server_analysis: dict, timeout: float, allow: bool, state: dict) -> None:
    if not allow:
        state["limitations"].append({
            "check": "render_diff",
            "reason": "Rendering was disabled for this run by --no-render.",
            "affected_checks": ["client_side_content_dependence"],
        })
        return

    render, load_error = load_sibling("render")

    if render is None:
        state["limitations"].append({
            "check": "render_diff",
            "reason": (
                f"{load_error or 'render.py is unavailable.'} Client-side content dependence was "
                "inferred from hydration markers instead of observed, so any related "
                "finding is reported at medium confidence."
            ),
            "affected_checks": ["client_side_content_dependence"],
        })
        state["status"] = "partial_failure"
        return

    result = render.render_page(url, timeout=timeout)

    if not result.get("available") or result.get("outcome") != "rendered":
        state["limitations"].append({
            "check": "render_diff",
            "reason": (
                f"Rendering did not complete: {result.get('error') or result.get('outcome')}. "
                "Rendered content could not be verified."
            ),
            "affected_checks": ["client_side_content_dependence"],
        })
        state["status"] = "partial_failure"
        return

    rendered_analysis = analyze_html(result.get("html", ""))
    server_chars = server_analysis["text_chars"]
    rendered_chars = rendered_analysis["text_chars"]
    gained = rendered_chars - server_chars

    state["observations"].append(
        f"Render comparison for {url}: server text {server_chars} chars, "
        f"rendered text {rendered_chars} chars, difference {gained}."
    )

    # Only a large proportional gap indicates the server response is a shell.
    if server_chars > 0 and rendered_chars >= server_chars * 3 and gained > THIN_TEXT_CHARS:
        state["findings"].append(finding(
            local_id="CR-007",
            title="Most page content appears only after client-side rendering",
            category="rendering",
            stage="renderable",
            evidence=[
                f"{url}: server response contained {server_chars} characters of "
                f"extractable text; rendered document contained {rendered_chars}.",
                f"{gained} characters of content are unavailable to a reader that "
                "does not execute JavaScript.",
            ],
            stage_block=3,
            fact_criticality=3,
            blast_radius=1,
            scope="single_page",
            affected_urls=[url],
            action_summary=(
                "Server-render the primary content, or emit the same facts as JSON-LD "
                "in the initial response so they survive without execution."
            ),
            action_rationale=(
                "The gap between the two documents is exactly the set of facts that "
                "cannot be cited. Closing it is what makes the page quotable."
            ),
            effort="high",
            verify_by=(
                "Re-run this audit and confirm the server and rendered text lengths "
                "are within the same order of magnitude."
            ),
        ))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def run(url: str, user_agent: str, timeout: float, allow_render: bool) -> dict:
    state: dict = {
        "status": "success",
        "observations": [],
        "findings": [],
        "limitations": [],
    }

    robots_result = stage_robots(url, user_agent, timeout, state)

    permission = (robots_result or {}).get("crawl_permission")
    target = (robots_result or {}).get("input_url", url)

    if robots_result is None:
        # Permission unknown because the check itself was unavailable.
        state["limitations"].append({
            "check": "http_retrieval",
            "reason": "Crawl permission could not be established, so retrieval was not attempted.",
            "affected_checks": ["server_html_extractability", "render_diff"],
        })
        return finalize(state)

    if permission == "disallowed":
        state["limitations"].append({
            "check": "http_retrieval",
            "reason": (
                "The target path is disallowed by robots.txt; page retrieval was "
                "deliberately not attempted."
            ),
            "affected_checks": ["server_html_extractability", "render_diff"],
        })
        return finalize(state)

    if permission == "unknown":
        state["limitations"].append({
            "check": "http_retrieval",
            "reason": (
                "robots.txt could not be retrieved, so crawl permission is unverified "
                "and retrieval was not attempted."
            ),
            "affected_checks": ["server_html_extractability", "render_diff"],
        })
        state["status"] = "partial_failure"
        return finalize(state)

    fetched = stage_fetch(target, user_agent, timeout, state)
    if fetched is None:
        return finalize(state)

    analysis = stage_extractability(target, fetched.get("body") or "", state)
    stage_render(target, analysis, timeout, allow_render, state)

    return finalize(state)


def finalize(state: dict) -> dict:
    return {
        "skill": SKILL_ID,
        "status": state["status"],
        "observations": state["observations"],
        "findings": sorted(state["findings"], key=lambda f: f["local_id"]),
        "limitations": state["limitations"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit crawl permission, retrieval, rendering and machine readability."
    )
    parser.add_argument("url", help="Target URL or bare domain.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--budget-ms", type=int, default=DEFAULT_BUDGET_MS)
    parser.add_argument("--sample-file", default=None)
    parser.add_argument("--no-render", action="store_true")

    args = parser.parse_args()

    if not args.url or not args.url.strip():
        print(json.dumps({
            "skill": SKILL_ID,
            "status": "failure",
            "observations": [],
            "findings": [],
            "limitations": [{
                "check": "input_validation",
                "reason": "No target URL supplied.",
                "affected_checks": ["all"],
            }],
        }, indent=2))
        return 2

    started = time.monotonic()

    try:
        result = run(
            url=args.url.strip(),
            user_agent=args.user_agent,
            timeout=args.timeout,
            allow_render=not args.no_render,
        )
    except Exception as exc:  # pragma: no cover - fault isolation boundary
        result = {
            "skill": SKILL_ID,
            "status": "failure",
            "observations": [],
            "findings": [],
            "limitations": [{
                "check": "specialist_execution",
                "reason": f"Unhandled error during audit: {type(exc).__name__}: {exc}",
                "affected_checks": ["all"],
            }],
        }

    elapsed_ms = int((time.monotonic() - started) * 1000)
    result["observations"].append(f"Specialist wall time: {elapsed_ms} ms.")

    if elapsed_ms > args.budget_ms:
        result["limitations"].append({
            "check": "time_budget",
            "reason": f"Specialist exceeded its budget of {args.budget_ms} ms.",
            "affected_checks": [],
        })

    print(json.dumps(result, indent=2, ensure_ascii=False))

    return 0 if result["status"] in ("success", "partial_failure") else 1


if __name__ == "__main__":
    sys.exit(main())