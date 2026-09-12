#!/usr/bin/env python3
"""
check.py -- evidence-only specialist for the crawl-render-audit skill.

This specialist consumes the canonical evidence.json produced by
audit-orchestrator/scripts/evidence.py.

Collector collects. Specialist reasons.

This module does NOT:
    - make network requests;
    - import fetch.py, robots.py, render.py or sitemap.py;
    - parse raw HTML again;
    - assign severity labels;
    - assign global finding IDs;
    - modify the target site.

It emits one specialist result object to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Optional


SKILL_ID = "crawl-render-audit"

DEFAULT_USER_AGENT = (
    "BrandAIReadinessAuditBot/1.0 "
    "(+https://github.com/KrishnaKapoor612/rohan_branc_ai_readiness)"
)

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_BUDGET_MS = 60_000
THIN_TEXT_CHARS = 600


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
        "affected_urls": sorted(set(affected_urls)),
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


def load_evidence(path: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    """
    Load the canonical evidence document.

    Returns (document, error). No network access occurs here.
    """
    if not path:
        return None, "No --evidence-file was supplied."

    if not os.path.isfile(path):
        return None, f"Evidence file was not found at {path}."

    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except Exception as exc:
        return None, (
            f"Could not read evidence.json: "
            f"{type(exc).__name__}: {exc}"
        )

    if not isinstance(document, dict):
        return None, "Evidence file does not contain a JSON object."

    return document, None


def stage_robots(evidence: dict, state: dict) -> None:
    """Reason about robots evidence already collected by evidence.py."""

    site_level = evidence.get("site_level") or {}
    robots = site_level.get("robots") or {}
    pages = evidence.get("pages") or []

    if not robots:
        state["limitations"].append({
            "check": "robots_policy",
            "reason": "Shared evidence contains no site-level robots data.",
            "affected_checks": [
                "crawl_permission",
                "ai_agent_directives",
            ],
        })
        state["status"] = "partial_failure"
        return

    if not robots.get("retrieved"):
        reason = robots.get("reason") or (
            "robots.txt was not retrieved successfully."
        )
        state["limitations"].append({
            "check": "robots_policy",
            "reason": reason,
            "affected_checks": [
                "crawl_permission",
                "ai_agent_directives",
            ],
        })
        state["status"] = "partial_failure"

    http_status = robots.get("http_status")
    robots_url = robots.get("robots_url")

    if robots.get("retrieved"):
        state["observations"].append(
            f"robots.txt at {robots_url} returned HTTP {http_status}."
        )

    # Page-level robots decisions are authoritative for the sampled URLs.
    disallowed_pages = [
        page for page in pages
        if page.get("robots_allowed") is False
    ]

    if disallowed_pages:
        affected_urls = sorted(
            page.get("url")
            for page in disallowed_pages
            if page.get("url")
        )

        state["findings"].append(finding(
            local_id="CR-001",
            title="Sampled page paths are disallowed by robots.txt",
            category="crawl_policy",
            stage="reachable",
            evidence=[
                f"{url}: robots_allowed=false."
                for url in affected_urls
            ],
            stage_block=4,
            fact_criticality=3,
            blast_radius=2 if len(affected_urls) > 1 else 1,
            scope="section" if len(affected_urls) > 1 else "single_page",
            affected_urls=affected_urls,
            action_summary=(
                "Allow publicly discoverable paths in robots.txt while "
                "keeping genuinely private paths blocked."
            ),
            action_rationale=(
                "A crawler that is refused access cannot reach the later "
                "stages of discovery."
            ),
            effort="low",
            verify_by=(
                "Re-run the audit and confirm the affected sampled paths "
                "have robots_allowed=true."
            ),
        ))

    ai_directives = robots.get("ai_agent_directives") or []
    blocked_agents = [
        entry for entry in ai_directives
        if entry.get("crawl_allowed") is False
    ]

    if blocked_agents:
        names = sorted(
            str(entry.get("agent"))
            for entry in blocked_agents
            if entry.get("agent")
        )

        evidence_lines = [
            f"AI crawler token {name} is recorded as crawl_allowed=false."
            for name in names
        ]

        for entry in blocked_agents:
            rule = entry.get("matched_rule")
            if rule:
                evidence_lines.append(
                    f"{entry.get('agent')}: "
                    f"{json.dumps(rule, sort_keys=True)}"
                )

        state["findings"].append(finding(
            local_id="CR-002",
            title="AI assistant crawlers are disallowed by robots.txt",
            category="crawl_policy",
            stage="reachable",
            evidence=evidence_lines,
            stage_block=4,
            fact_criticality=3,
            blast_radius=3,
            scope="site_wide",
            affected_urls=[
                evidence.get("site") or ""
            ],
            action_summary=(
                "Review robots.txt rules for assistant crawler tokens and "
                "allow access to public content intended for discovery."
            ),
            action_rationale=(
                "A crawler that cannot retrieve public content cannot use "
                "that content when constructing answers."
            ),
            effort="low",
            verify_by=(
                "Re-run the audit and confirm the affected AI crawler "
                "tokens are no longer recorded as disallowed."
            ),
        ))


def stage_retrieval(evidence: dict, state: dict) -> None:
    """Reason about page retrieval outcomes already collected."""

    pages = evidence.get("pages") or []

    if not pages:
        state["limitations"].append({
            "check": "http_retrieval",
            "reason": "No page evidence was collected.",
            "affected_checks": [
                "server_html_extractability",
                "render_diff",
            ],
        })
        state["status"] = "partial_failure"
        return

    failed_pages = [
        page for page in pages
        if page.get("status") is not None
        and not page.get("html")
        and (
            page.get("error")
            or page.get("limitations")
        )
    ]

    for page in failed_pages:
        url = page.get("url", "")
        error = page.get("error") or {}
        message = (
            error.get("message")
            if isinstance(error, dict)
            else str(error)
        )

        state["limitations"].append({
            "check": "http_retrieval",
            "reason": (
                f"Page evidence for {url} was unavailable: "
                f"{message or 'no HTML evidence was collected'}."
            ),
            "affected_checks": [
                "server_html_extractability",
                "render_diff",
            ],
        })

    if failed_pages:
        state["status"] = "partial_failure"

    for page in pages:
        if page.get("status") is not None:
            state["observations"].append(
                f"Retrieved {page.get('url')}: "
                f"HTTP {page.get('status')}, "
                f"content_type={page.get('content_type')}."
            )


def stage_extractability(evidence: dict, state: dict) -> None:
    """Reason about already-normalized page evidence."""

    pages = evidence.get("pages") or []

    for page in pages:
        url = page.get("url", "")
        page_structure = page.get("page_structure") or {}
        jsonld = page.get("jsonld") or []
        title = page.get("title")

        if not page.get("html"):
            continue

        text_chars = page_structure.get("text_chars", 0)
        hydration_markers = page_structure.get(
            "hydration_markers", []
        )

        # A thin server response plus explicit hydration markers is a
        # useful signal, but without rendered evidence it remains medium
        # confidence.
        if text_chars < THIN_TEXT_CHARS and hydration_markers:
            state["findings"].append(finding(
                local_id=f"CR-003-{len(state['findings']) + 1}",
                title=(
                    "Page content appears dependent on client-side rendering"
                ),
                category="rendering",
                stage="renderable",
                evidence=[
                    f"{url}: server response contains "
                    f"{text_chars} characters of extractable text.",
                    "Hydration markers: "
                    + ", ".join(sorted(hydration_markers)),
                ],
                stage_block=3,
                fact_criticality=3,
                blast_radius=1,
                scope="single_page",
                affected_urls=[url],
                action_summary=(
                    "Server-render the primary content or expose the key "
                    "facts in the initial HTML response."
                ),
                action_rationale=(
                    "A non-executing reader cannot quote facts that only "
                    "appear after client-side execution."
                ),
                effort="high",
                verify_by=(
                    "Re-run the audit and confirm the key content is "
                    "present in the initial HTML response."
                ),
                confidence="medium",
            ))

        unparseable = [
            block for block in jsonld
            if block.get("parsed") is False
        ]

        if unparseable:
            state["findings"].append(finding(
                local_id=f"CR-004-{len(state['findings']) + 1}",
                title="Structured data is present but not parseable",
                category="structured_data",
                stage="extractable",
                evidence=[
                    f"{url}: {len(unparseable)} JSON-LD block(s) "
                    "were recorded as unparseable."
                ],
                stage_block=2,
                fact_criticality=3,
                blast_radius=1,
                scope="single_page",
                affected_urls=[url],
                action_summary=(
                    "Fix malformed JSON-LD and validate the generated "
                    "markup before deployment."
                ),
                action_rationale=(
                    "Invalid structured data cannot be reliably consumed "
                    "by machine readers."
                ),
                effort="low",
                verify_by=(
                    "Re-run the audit and confirm all JSON-LD blocks "
                    "are parseable."
                ),
            ))

        # Absence of JSON-LD is deliberately NOT a finding.
        # It is an observation only because structured data is useful but
        # not universally required for AI discoverability.
        if not jsonld:
            state["observations"].append(
                f"{url}: no JSON-LD evidence was collected. "
                "This is not treated as a defect by itself."
            )

        if not title:
            state["findings"].append(finding(
                local_id=f"CR-005-{len(state['findings']) + 1}",
                title="Page has no title element",
                category="machine_readability",
                stage="extractable",
                evidence=[
                    f"{url}: no non-empty title was recorded."
                ],
                stage_block=2,
                fact_criticality=3,
                blast_radius=1,
                scope="single_page",
                affected_urls=[url],
                action_summary=(
                    "Give the page a unique title describing the entity "
                    "and subject of the page."
                ),
                action_rationale=(
                    "A page title provides a concise machine-readable "
                    "statement of the document's subject."
                ),
                effort="low",
                verify_by=(
                    "Re-run the audit and confirm a non-empty title "
                    "is recorded."
                ),
            ))


def stage_render(evidence: dict, state: dict, render_allowed: bool) -> None:
    """Reason about render evidence already collected."""

    if not render_allowed:
        state["limitations"].append({
            "check": "render_diff",
            "reason": (
                "Rendering was disabled for this run by --no-render."
            ),
            "affected_checks": [
                "client_side_content_dependence"
            ],
        })
        return

    pages = evidence.get("pages") or []

    render_evidence_seen = False

    for page in pages:
        render = page.get("render") or {}

        if not render:
            continue

        render_evidence_seen = True

        if not render.get("attempted"):
            continue

        if not render.get("available"):
            state["limitations"].append({
                "check": "render_diff",
                "reason": (
                    f"Rendered evidence was unavailable for "
                    f"{page.get('url', '')}."
                ),
                "affected_checks": [
                    "client_side_content_dependence"
                ],
            })
            state["status"] = "partial_failure"
            continue

        signals = render.get("signals") or {}
        server_chars = signals.get("server_text_chars")
        rendered_chars = signals.get("text_chars")
        gained = signals.get("gained_chars")

        if not isinstance(server_chars, int):
            continue
        if not isinstance(rendered_chars, int):
            continue
        if not isinstance(gained, int):
            continue

        state["observations"].append(
            f"Render comparison for {page.get('url', '')}: "
            f"server={server_chars}, rendered={rendered_chars}, "
            f"gained={gained}."
        )

        if (
            server_chars > 0
            and rendered_chars >= server_chars * 3
            and gained > THIN_TEXT_CHARS
        ):
            state["findings"].append(finding(
                local_id=f"CR-006-{len(state['findings']) + 1}",
                title=(
                    "Most page content appears only after client-side rendering"
                ),
                category="rendering",
                stage="renderable",
                evidence=[
                    f"{page.get('url', '')}: server response contained "
                    f"{server_chars} characters of extractable text; "
                    f"rendered content contained {rendered_chars}.",
                    f"{gained} additional characters appeared after "
                    "rendering."
                ],
                stage_block=3,
                fact_criticality=3,
                blast_radius=1,
                scope="single_page",
                affected_urls=[page.get("url", "")],
                action_summary=(
                    "Server-render the primary content or expose key facts "
                    "in the initial HTML response."
                ),
                action_rationale=(
                    "Content added only after execution may be unavailable "
                    "to readers that do not execute JavaScript."
                ),
                effort="high",
                verify_by=(
                    "Re-run the audit and confirm the server and rendered "
                    "content are substantially closer."
                ),
                confidence="high",
            ))

    if pages and not render_evidence_seen:
        state["limitations"].append({
            "check": "render_diff",
            "reason": (
                "No render evidence was available in the shared evidence."
            ),
            "affected_checks": [
                "client_side_content_dependence"
            ],
        })
        state["status"] = "partial_failure"


def finalize(state: dict) -> dict:
    """Return deterministic specialist output."""

    return {
        "skill": SKILL_ID,
        "status": state["status"],
        "observations": sorted(state["observations"]),
        "findings": sorted(
            state["findings"],
            key=lambda item: item["local_id"],
        ),
        "limitations": sorted(
            state["limitations"],
            key=lambda item: (
                item.get("check", ""),
                item.get("reason", ""),
            ),
        ),
        "proactive_recommendations": [],
    }


def run(
    evidence: dict,
    allow_render: bool,
) -> dict:
    state = {
        "status": "success",
        "observations": [],
        "findings": [],
        "limitations": [],
    }

    pages = evidence.get("pages")

    if not isinstance(pages, list):
        state["limitations"].append({
            "check": "evidence_input",
            "reason": (
                "Shared evidence does not contain a valid pages list."
            ),
            "affected_checks": ["all"],
        })
        state["status"] = "failure"
        return finalize(state)

    if not pages:
        state["limitations"].append({
            "check": "evidence_input",
            "reason": (
                "Shared evidence contains no pages. No website findings "
                "were inferred from missing evidence."
            ),
            "affected_checks": ["all"],
        })
        state["status"] = "partial_failure"
        return finalize(state)

    stage_robots(evidence, state)
    stage_retrieval(evidence, state)
    stage_extractability(evidence, state)
    stage_render(evidence, state, allow_render)

    return finalize(state)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze shared crawl/render evidence without making "
            "network requests."
        )
    )

    parser.add_argument(
        "url",
        help="Target URL or bare domain.",
    )

    parser.add_argument(
        "--evidence-file",
        default=None,
        help=(
            "Path to the shared evidence.json. "
            "Canonical specialist input."
        ),
    )

    parser.add_argument(
        "--sample-file",
        default=None,
        help=(
            "Deprecated compatibility option. Accepted so older "
            "orchestrators do not fail argument parsing."
        ),
    )

    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )

    parser.add_argument(
        "--budget-ms",
        type=int,
        default=DEFAULT_BUDGET_MS,
    )

    parser.add_argument(
        "--no-render",
        action="store_true",
    )

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
            "proactive_recommendations": [],
        }, indent=2))
        return 2

    started = time.monotonic()

    evidence_path = args.evidence_file

    # --evidence-file is canonical. --sample-file is intentionally not
    # loaded because the new contract uses normalized evidence.json.
    evidence, error = load_evidence(evidence_path)

    if evidence is None:
        result = {
            "skill": SKILL_ID,
            "status": "failure",
            "observations": [],
            "findings": [],
            "limitations": [{
                "check": "evidence_input",
                "reason": error or "Shared evidence is unavailable.",
                "affected_checks": ["all"],
            }],
            "proactive_recommendations": [],
        }
    else:
        try:
            result = run(
                evidence=evidence,
                allow_render=not args.no_render,
            )
        except Exception as exc:
            result = {
                "skill": SKILL_ID,
                "status": "failure",
                "observations": [],
                "findings": [],
                "limitations": [{
                    "check": "specialist_execution",
                    "reason": (
                        f"Unhandled error during evidence analysis: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "affected_checks": ["all"],
                }],
                "proactive_recommendations": [],
            }

    elapsed_ms = int((time.monotonic() - started) * 1000)

    result["observations"].append(
        f"Specialist wall time: {elapsed_ms} ms."
    )

    if elapsed_ms > args.budget_ms:
        result["limitations"].append({
            "check": "time_budget",
            "reason": (
                f"Specialist exceeded its budget of "
                f"{args.budget_ms} ms."
            ),
            "affected_checks": [],
        })

    print(json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
    ))

    return (
        0
        if result["status"] in {"success", "partial_failure"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())