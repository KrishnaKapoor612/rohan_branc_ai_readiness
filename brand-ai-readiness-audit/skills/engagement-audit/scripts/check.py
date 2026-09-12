#!/usr/bin/env python3
"""
check.py -- specialist coordinator for the engagement-audit skill.

Owns the post-arrival half of the problem: a visitor arrives mid-journey,
already primed with a specific question an assistant answered, and the site has
no idea what that question was.

This specialist consumes the shared evidence.json produced by audit-orchestrator.
It does not fetch pages or import robots.py/fetch.py/render.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit

SKILL_ID = "engagement-audit"
DEFAULT_USER_AGENT = (
    "BrandAIReadinessAuditBot/1.0 "
    "(+https://github.com/KrishnaKapoor612/rohan_branc_ai_readiness)"
)
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_BUDGET_MS = 60_000
MIN_INTERNAL_LINKS = 5
MODAL_MARKERS = re.compile(
    r'(role\s*=\s*["\']dialog["\']|aria-modal\s*=\s*["\']true["\']'
    r'|class\s*=\s*["\'][^"\']*(modal|overlay|backdrop|lightbox)[^"\']*["\'])',
    re.I,
)
CONSENT_VOCABULARY = re.compile(
    r"(cookie[- ]?(consent|banner|notice|policy)|gdpr|ccpa"
    r"|accept all cookies|manage (your )?preferences|privacy (choices|settings))",
    re.I,
)
SCROLL_LOCK = re.compile(
    r"(body|html)[^{}]*\{[^{}]*overflow\s*:\s*hidden", re.I
)
SEARCH_INPUT = re.compile(
    r'<input[^>]+(type\s*=\s*["\']search["\']'
    r'|name\s*=\s*["\'](q|s|query|search|keyword|term)["\'])',
    re.I,
)
BREADCRUMB_MARKUP = re.compile(
    r'(BreadcrumbList|aria-label\s*=\s*["\'][^"\']*breadcrumb'
    r'|class\s*=\s*["\'][^"\']*breadcrumb)',
    re.I,
)
MAIN_LANDMARK = re.compile(
    r'(<main\b|<article\b|role\s*=\s*["\']main["\'])', re.I
)
THIN_PAGE_WORDS = 120


class _TextExtractor(HTMLParser):
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
        if self._depth == 0 and data.strip():
            self.chunks.append(data.strip())

    def text(self) -> str:
        return " ".join(self.chunks)


def visible_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def load_evidence(path: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    if not path:
        return None, "No --evidence-file was supplied."
    try:
        with open(path, "r", encoding="utf-8") as handle:
            evidence = json.load(handle)
    except Exception as exc:
        return None, f"Evidence file at {path} could not be read ({type(exc).__name__}: {exc})."
    if not isinstance(evidence, dict):
        return None, f"Evidence file at {path} did not contain a JSON object."
    if not isinstance(evidence.get("pages"), list):
        return None, "Shared evidence is missing the pages array."
    return evidence, None


def _html_content(page: dict) -> Optional[str]:
    html = page.get("html")
    if isinstance(html, dict):
        content = html.get("content")
        return content if isinstance(content, str) and content else None
    if isinstance(html, str) and html:
        return html
    return None


def analyze_orientation(url: str, page: dict) -> dict:
    """
    Preserve the existing orientation analysis, but consume normalized evidence
    first. Raw HTML is used only for signals evidence.py does not currently
    expose separately (modal/scroll-lock/main landmark/breadcrumb/word count).
    """
    headings = page.get("headings") or []
    if not isinstance(headings, list):
        headings = []
    headings = [h for h in headings if isinstance(h, dict)]

    engagement = page.get("engagement_signals") or {}
    structure = page.get("page_structure") or {}
    links = page.get("links") or {}

    h2_h3_count = sum(1 for h in headings if h.get("level") in (2, 3))
    anchored = sum(
        1 for h in headings
        if h.get("level") in (2, 3) and h.get("has_id") is True
    )

    internal_value = links.get("internal_count")
    internal_links = int(internal_value) if isinstance(internal_value, (int, float)) else 0

    h1_value = structure.get("h1_count")
    h1_count = int(h1_value) if isinstance(h1_value, (int, float)) else 0

    search_value = engagement.get("search_ui_detected")
    has_search_entry = bool(search_value) if isinstance(search_value, bool) else None

    html = _html_content(page)
    words: Optional[int] = None
    has_main_landmark: Optional[bool] = None
    has_breadcrumb: Optional[bool] = None
    consent_wall: Optional[bool] = None
    scroll_locked: Optional[bool] = None

    if html is not None:
        text = visible_text(html)
        words = len(text.split())
        has_modal = bool(MODAL_MARKERS.search(html))
        has_consent_words = bool(CONSENT_VOCABULARY.search(html))
        consent_wall = has_modal and has_consent_words
        scroll_locked = bool(SCROLL_LOCK.search(html))
        has_main_landmark = bool(MAIN_LANDMARK.search(html))
        has_breadcrumb = bool(BREADCRUMB_MARKUP.search(html))

    return {
        "url": url,
        "words": words,
        "h1_count": h1_count,
        "h2_h3_count": h2_h3_count,
        "anchored_headings": anchored,
        "internal_links": internal_links,
        "has_main_landmark": has_main_landmark,
        "has_search_entry": has_search_entry,
        "has_breadcrumb": has_breadcrumb,
        "consent_wall": consent_wall,
        "scroll_locked": scroll_locked,
        "has_html_evidence": html is not None,
    }


def scope_from_ratio(affected: int, total: int) -> tuple[str, int]:
    if total <= 0:
        return "single_page", 1
    ratio = affected / total
    if total >= 3 and ratio >= 0.8:
        return "site_wide", 3
    if total >= 3 and ratio >= 0.4:
        return "section", 2
    if affected > 1:
        return "section", 2
    return "single_page", 1


def site_scope(total: int) -> tuple[str, int]:
    if total >= 3:
        return "site_wide", 3
    if total == 2:
        return "section", 2
    return "single_page", 1


def finding(local_id, title, category, evidence, stage_block, fact_criticality,
            scope, blast_radius, affected_urls, action_summary, action_rationale,
            effort, verify_by, confidence="high", stage="engaging") -> dict:
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


def evaluate(analyses: dict, state: dict) -> None:
    pages = sorted(analyses)
    total = len(pages)
    if not total:
        return

    # --- 1. Deep-link addressability -------------------------------------
    unanchored = [
        u for u in pages
        if analyses[u]["h2_h3_count"] > 0
        and analyses[u]["anchored_headings"] == 0
    ]
    with_headings = [u for u in pages if analyses[u]["h2_h3_count"] > 0]
    if with_headings and len(unanchored) == len(with_headings):
        scope, radius = scope_from_ratio(len(unanchored), len(with_headings))
        state["findings"].append(finding(
            local_id="EN-001",
            title="Section headings carry no id, so no page can be deep-linked to its answer",
            category="engagement_orientation",
            evidence=[
                f"{len(unanchored)} of {len(with_headings)} sampled page(s) with "
                "h2/h3 headings expose no id attribute on any of them.",
                "Example: " + unanchored[0],
                "Without a stable anchor an assistant can only link the page top, "
                "so a visitor primed with a specific question lands above the answer.",
            ],
            stage_block=2, fact_criticality=2, scope=scope, blast_radius=radius,
            affected_urls=unanchored[:12],
            action_summary=(
                "Emit a stable, slugified id on every h2 and h3 in body content, and "
                "keep the ids unchanged across deploys. Serve them in the initial HTML "
                "so a fragment link resolves before any script runs."
            ),
            action_rationale=(
                "Assistants cite a page and link to it. If the only addressable target "
                "is the document root, the visitor arrives at the top of a long page "
                "and has to search for what the assistant already told them, which is "
                "the moment the bounce happens."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm anchored_headings is non-zero on sampled content pages.",
        ))

    # --- 2. Consent or interstitial wall ---------------------------------
    walled = [u for u in pages if analyses[u]["consent_wall"] is True]
    locked = [u for u in pages if analyses[u]["scroll_locked"] is True]
    blocking = sorted(set(walled) & set(locked))
    if blocking:
        scope, radius = scope_from_ratio(len(blocking), total)
        state["findings"].append(finding(
            local_id="EN-002",
            title="A consent interstitial covers the content before a visitor can read it",
            category="context_continuity",
            evidence=[
                f"{len(blocking)} of {total} sampled page(s) ship a modal structure "
                "together with consent vocabulary in the initial HTML.",
                f"The same page(s) also apply a scroll lock to body or html: {blocking[0]}",
            ],
            stage_block=2, fact_criticality=3, scope=scope, blast_radius=radius,
            affected_urls=blocking[:12],
            action_summary=(
                "Render the page content underneath the consent layer rather than "
                "behind it, drop the scroll lock, and preserve any inbound fragment "
                "or query parameter across the dismissal so the visitor is returned "
                "to the section they arrived for."
            ),
            action_rationale=(
                "A visitor sent by an assistant arrives with a specific question and "
                "a few seconds of patience. An opaque layer between arrival and answer "
                "spends all of it, and a scroll lock also destroys any fragment target "
                "the assistant linked to."
            ),
            effort="medium",
            verify_by="Re-run this audit and confirm no sampled page combines a consent modal with a scroll lock.",
        ))
    elif walled:
        state["observations"].append(
            f"{len(walled)} of {total} sampled page(s) contain consent vocabulary "
            "inside a modal structure, but no scroll lock was observed. Recorded as "
            "an observation: a dismissible banner is not by itself a blocking wall."
        )

    # --- 3. No content anchor for an arriving visitor ---------------------
    unanchored_pages = [
        u for u in pages
        if analyses[u]["has_main_landmark"] is False
        and analyses[u]["h1_count"] == 0
    ]
    if unanchored_pages:
        scope, radius = scope_from_ratio(len(unanchored_pages), total)
        state["findings"].append(finding(
            local_id="EN-003",
            title="Pages expose no main landmark and no h1, so nothing states what the page answers",
            category="engagement_orientation",
            evidence=[
                f"{len(unanchored_pages)} of {total} sampled page(s) contain neither "
                "a main/article landmark nor an h1 element.",
                "Example: " + unanchored_pages[0],
            ],
            stage_block=2, fact_criticality=2, scope=scope, blast_radius=radius,
            affected_urls=unanchored_pages[:12],
            action_summary=(
                "Give every page one h1 naming the specific thing it covers, and wrap "
                "the primary content in a main or article landmark so the first "
                "readable block answers the page's implied question."
            ),
            action_rationale=(
                "Answer-first structure serves both readers at once. It gives a "
                "visitor immediate confirmation they are in the right place, and it "
                "gives an assistant a clean, quotable opening claim."
            ),
            effort="medium",
            verify_by="Re-run this audit and confirm h1_count is at least 1 on each page.",
        ))

    # --- 4. No recovery path when the visitor lands wrong ------------------
    search_known = [u for u in pages if analyses[u]["has_search_entry"] is not None]
    if search_known and len(search_known) == total and total >= 2:
        if not any(analyses[u]["has_search_entry"] for u in pages):
            state["findings"].append(finding(
                local_id="EN-004",
                title="No on-site search entry point, so a visitor who lands on the wrong page has no recovery",
                category="context_continuity",
                evidence=[
                    f"0 of {total} sampled page(s) expose a search input in the initial HTML.",
                    "Checked for input[type=search] and inputs named q, s, query, search, keyword or term.",
                ],
                stage_block=2, fact_criticality=2,
                scope=site_scope(total)[0], blast_radius=site_scope(total)[1],
                affected_urls=pages[:12],
                action_summary=(
                    "Add a site search form present in the server HTML, with a GET action "
                    "and a stable query parameter, so an assistant can hand a visitor "
                    "straight to a results URL for the question they asked."
                ),
                action_rationale=(
                    "A search endpoint with a readable query parameter is the cheapest "
                    "possible context handoff: the intent the visitor arrived with becomes "
                    "a linkable URL instead of being discarded at the door."
                ),
                effort="medium",
                verify_by="Re-run this audit and confirm has_search_entry is true.",
            ))

    # --- 5. No crawlable navigation onward ---------------------------------
    starved = [u for u in pages if analyses[u]["internal_links"] < MIN_INTERNAL_LINKS]
    if starved:
        scope, radius = scope_from_ratio(len(starved), total)
        state["findings"].append(finding(
            local_id="EN-005",
            title="Server HTML contains almost no internal links, leaving no navigable path onward",
            category="engagement_orientation",
            evidence=[
                f"{len(starved)} of {total} sampled page(s) expose fewer than "
                f"{MIN_INTERNAL_LINKS} distinct same-origin links in the initial response.",
                f"Lowest observed: {min(analyses[u]['internal_links'] for u in starved)} "
                f"link(s) on {sorted(starved, key=lambda u: analyses[u]['internal_links'])[0]}",
            ],
            stage_block=2, fact_criticality=2, scope=scope, blast_radius=radius,
            affected_urls=starved[:12],
            action_summary=(
                "Serve the primary navigation and contextual in-content links as real "
                "anchor elements in the initial HTML rather than building them after hydration."
            ),
            action_rationale=(
                "Navigation assembled client-side is invisible to a crawler and slow "
                "for a visitor. Both readers end up on a page with no onward path, "
                "which is a dead end for discovery and for the session."
            ),
            effort="medium",
            verify_by=f"Re-run this audit and confirm internal_links is at or above {MIN_INTERNAL_LINKS} on sampled pages.",
        ))

    # --- 6. Breadcrumbs, reported only where they would matter -------------
    deep = [u for u in pages if (urlsplit(u).path or "/").strip("/").count("/") >= 1]
    deep_known = [u for u in deep if analyses[u]["has_breadcrumb"] is not None]
    if len(deep_known) >= 2 and not any(analyses[u]["has_breadcrumb"] for u in deep_known):
        state["findings"].append(finding(
            local_id="EN-006",
            title="Deep pages carry no breadcrumb trail",
            category="engagement_orientation",
            evidence=[
                f"0 of {len(deep_known)} sampled page(s) at depth 2 or greater expose "
                "breadcrumb markup or BreadcrumbList structured data.",
                "Example: " + sorted(deep_known)[0],
            ],
            stage_block=2, fact_criticality=1, scope="section", blast_radius=2,
            affected_urls=sorted(deep_known)[:12],
            action_summary="Add a breadcrumb trail with BreadcrumbList JSON-LD on pages below the top level.",
            action_rationale=(
                "A visitor delivered straight to a deep page has no sense of where "
                "they are in the catalogue. Breadcrumbs supply that in one line and "
                "simultaneously tell a machine how the page sits in the hierarchy."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm has_breadcrumb on deep pages.",
        ))

    # --- Thin content: observation, never a finding ------------------------
    thin = [u for u in pages if analyses[u]["words"] is not None and analyses[u]["words"] < THIN_PAGE_WORDS]
    if thin:
        state["observations"].append(
            f"{len(thin)} of {total} sampled page(s) carry fewer than "
            f"{THIN_PAGE_WORDS} words of readable text in the server response. "
            "Recorded as an observation only: thin server HTML may reflect "
            "client-side rendering, which crawl-render-audit owns."
        )


def proactive(analyses: dict) -> list[dict]:
    return [
        {
            "title": "Accept and use the intent a visitor arrives with",
            "summary": (
                "Read the inbound text fragment, query string and referrer on page "
                "load, and render a short orientation strip at the top of the page "
                "that names what the visitor appears to be looking for and links straight to it."
            ),
            "rationale": (
                "The assistant knows the question; today the site learns nothing about "
                "it. Turning that signal into one visible line converts a generic "
                "landing into a continuation of the conversation the visitor was already having."
            ),
            "priority": "high",
            "category": "context_continuity",
        },
        {
            "title": "Publish answer-shaped blocks for the questions assistants receive",
            "summary": (
                "For each recurring question in the category, publish a short, "
                "self-contained block with its own anchored heading and a first "
                "sentence that answers it outright, then expand below."
            ),
            "rationale": (
                "An extractable answer block serves the assistant a quotable claim and "
                "gives the arriving visitor an anchor to be linked to. One change "
                "improves citation and bounce at the same time."
            ),
            "priority": "medium",
            "category": "engagement_orientation",
        },
        {
            "title": "Support text fragment links end to end",
            "summary": (
                "Verify that URLs ending in a text fragment directive scroll to and "
                "highlight the matching passage, and that no scroll hijacking, lazy "
                "mounting or consent layer prevents it."
            ),
            "rationale": (
                "Text fragments let an assistant link to a sentence rather than a page "
                "even where no id exists. Most sites break them accidentally, and the "
                "breakage is invisible until someone tests it."
            ),
            "priority": "medium",
            "category": "engagement_orientation",
        },
    ]


def audit_pages(evidence: dict, state: dict) -> dict:
    analyses: dict[str, dict] = {}
    pages = evidence.get("pages") or []
    failed = 0
    missing_html = 0

    valid_pages = sorted(
        [p for p in pages if isinstance(p, dict) and isinstance(p.get("url"), str)],
        key=lambda p: p["url"],
    )
    for page in valid_pages:
        url = page["url"]
        status = page.get("status")
        error = page.get("error")
        html = _html_content(page)

        if error or (status is not None and not (200 <= int(status) < 300)):
            failed += 1
            continue
        if html is None:
            missing_html += 1
            continue

        analyses[url] = analyze_orientation(url, page)

    state["observations"].append(
        f"Analysed {len(analyses)} of {len(valid_pages)} sampled page(s) from shared evidence."
    )
    if failed:
        state["limitations"].append({
            "check": "page_retrieval",
            "reason": (
                f"{failed} sampled page(s) have retrieval errors or non-2xx status in "
                "evidence.json, so orientation could not be assessed for them."
            ),
            "affected_checks": ["deep_link_addressability", "orientation_structure"],
        })
        state["status"] = "partial_failure"
    if missing_html:
        state["limitations"].append({
            "check": "page_evidence",
            "reason": (
                f"{missing_html} sampled page(s) have no usable html evidence in "
                "evidence.json, so HTML-dependent orientation checks were not run for them."
            ),
            "affected_checks": [
                "consent_wall",
                "orientation_structure",
                "breadcrumb_detection",
            ],
        })
        state["status"] = "partial_failure"
    return analyses


def run(url, evidence_file, no_render) -> dict:
    state: dict = {
        "status": "success",
        "observations": [],
        "findings": [],
        "limitations": [],
        "proactive_recommendations": [],
    }

    evidence, error = load_evidence(evidence_file)
    if evidence is None:
        state["status"] = "failure"
        state["limitations"].append({
            "check": "evidence_input",
            "reason": error or "Shared evidence could not be loaded.",
            "affected_checks": ["all"],
        })
        return finalize(state)

    pages = evidence.get("pages") or []
    state["observations"].append(f"Shared evidence contains {len(pages)} sampled page(s).")

    if not pages:
        state["status"] = "partial_failure"
        state["limitations"].append({
            "check": "page_sample",
            "reason": "Shared evidence contains no sampled pages; no orientation checks were run.",
            "affected_checks": ["all_orientation_checks"],
        })
        return finalize(state)

    if no_render:
        state["observations"].append(
            "Rendering was disabled at invocation; engagement-audit reads only collected evidence."
        )

    analyses = audit_pages(evidence, state)
    evaluate(analyses, state)
    if analyses:
        state["proactive_recommendations"] = proactive(analyses)
    return finalize(state)


def finalize(state: dict) -> dict:
    return {
        "skill": SKILL_ID,
        "status": state["status"],
        "observations": state["observations"],
        "findings": sorted(state["findings"], key=lambda f: f["local_id"]),
        "limitations": state["limitations"],
        "proactive_recommendations": state.get("proactive_recommendations", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit post-arrival orientation and context continuity."
    )
    parser.add_argument("url")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--budget-ms", type=int, default=DEFAULT_BUDGET_MS)
    parser.add_argument("--evidence-file", default=None)
    parser.add_argument("--sample-file", default=None)
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()

    if not args.url or not args.url.strip():
        print(json.dumps({
            "skill": SKILL_ID, "status": "failure", "observations": [],
            "findings": [], "limitations": [{
                "check": "input_validation",
                "reason": "No target URL supplied.",
                "affected_checks": ["all"],
            }],
            "proactive_recommendations": [],
        }, indent=2))
        return 2

    started = time.monotonic()
    try:
        result = run(args.url.strip(), args.evidence_file, args.no_render)
    except Exception as exc:
        result = {
            "skill": SKILL_ID, "status": "failure", "observations": [],
            "findings": [], "limitations": [{
                "check": "specialist_execution",
                "reason": f"Unhandled error during audit: {type(exc).__name__}: {exc}",
                "affected_checks": ["all"],
            }],
            "proactive_recommendations": [],
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
