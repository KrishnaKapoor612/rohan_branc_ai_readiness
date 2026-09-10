#!/usr/bin/env python3
"""
check.py -- specialist coordinator for the freshness-corroboration-audit skill.

Owns the trust half of discoverability: identity, declared sameAs links and
freshness signals. Page collection is owned by audit-orchestrator/evidence.py.

This specialist does not import fetch.py or robots.py. The only network request
remaining here is the explicitly permitted, bounded verification of declared
sameAs URLs.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit

SKILL_ID = "freshness-corroboration-audit"
DEFAULT_USER_AGENT = (
    "BrandAIReadinessAuditBot/1.0 "
    "(+https://github.com/KrishnaKapoor612/brand-ai-readiness-audit)"
)
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_BUDGET_MS = 90_000
POLITE_DELAY_SECONDS = 0.4
MAX_SAMEAS_CHECKS = 3
MAX_SAMEAS_BYTES = 200 * 1024
MAX_REDIRECTS = 3

IDENTITY_TYPES = {
    "organization", "corporation", "localbusiness", "onlinestore",
    "store", "brand", "website", "ngo", "educationalorganization",
}
DATE_FIELDS = ("dateModified", "datePublished", "dateCreated", "uploadDate")

# Kept for compatibility with the old helper surface; evidence.py now owns
# extraction of these signals.
DATE_MARKUP = re.compile(
    r'(<time\b[^>]*datetime\s*=|itemprop\s*=\s*["\']date(Modified|Published)["\']'
    r'|property\s*=\s*["\']article:(modified|published)_time["\'])',
    re.I,
)
OG_SITE_NAME = re.compile(
    r'<meta[^>]+property\s*=\s*["\']og:site_name["\'][^>]+content\s*=\s*["\'](.*?)["\']',
    re.I | re.S,
)


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


def analyze_identity(url: str, page: dict) -> dict:
    """
    Preserve the old analysis outputs while consuming normalized evidence.

    evidence.py already flattened the useful JSON-LD facts into per-block
    types/names/same_as/dates, and separately exposes identity/date/OG signals.
    No HTML re-parsing is needed here.
    """
    jsonld = page.get("jsonld") or []
    if not isinstance(jsonld, list):
        jsonld = []

    identity_blocks = []
    jsonld_nodes = 0
    names = set()
    same_as = set()

    for block in jsonld:
        if not isinstance(block, dict):
            continue
        types = {
            str(t).strip().lower()
            for t in (block.get("types") or [])
            if isinstance(t, str) and t.strip()
        }
        # evidence.py records each JSON-LD block after flattening @graph for
        # its type/name/sameAs fields. Count the block as an identity-bearing
        # node when any recorded type is an identity type.
        jsonld_nodes += 1
        if types & IDENTITY_TYPES:
            identity_blocks.append(block)
        for name in block.get("names") or []:
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
        for value in block.get("same_as") or []:
            if isinstance(value, str) and value.strip():
                same_as.add(value.strip())

    identity_signals = page.get("identity_signals") or []
    for signal in identity_signals:
        if not isinstance(signal, dict):
            continue
        if signal.get("source") == "jsonld_name":
            value = signal.get("value")
            if isinstance(value, str) and value.strip():
                names.add(value.strip())

    open_graph = page.get("open_graph") or {}
    og_site_name = open_graph.get("og:site_name")
    if not isinstance(og_site_name, str) or not og_site_name.strip():
        og_site_name = None

    # Keep the shared normalized same_as list authoritative if present.
    page_same_as = page.get("same_as")
    if isinstance(page_same_as, list):
        same_as = {
            value.strip()
            for value in page_same_as
            if isinstance(value, str) and value.strip()
        }

    date_signals = page.get("date_signals")
    if not isinstance(date_signals, list):
        date_signals = []

    page_structure = page.get("page_structure") or {}
    text_chars = page_structure.get("text_chars")
    if not isinstance(text_chars, (int, float)):
        text_chars = 0

    return {
        "url": url,
        "jsonld_nodes": jsonld_nodes,
        "identity_nodes": len(identity_blocks),
        "identity_names": sorted(names),
        "same_as": sorted(same_as),
        "og_site_name": og_site_name,
        "has_date_signal": bool(date_signals),
        "text_chars": int(text_chars),
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


def finding(local_id, title, category, stage, evidence, stage_block,
            fact_criticality, scope, blast_radius, affected_urls,
            action_summary, action_rationale, effort, verify_by,
            confidence="high") -> dict:
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


def evaluate(analyses: dict, origin: str, state: dict) -> list[str]:
    """Returns the sameAs URLs worth verifying, if any."""
    pages = sorted(analyses)
    total = len(pages)
    if not total:
        return []

    site_wide_scope, site_wide_radius = site_scope(total)
    all_same_as = sorted({s for u in pages for s in analyses[u]["same_as"]})
    identity_pages = [u for u in pages if analyses[u]["identity_nodes"] > 0]

    # --- 1. No declared identity -----------------------------------------
    if not identity_pages:
        state["findings"].append(finding(
            local_id="FC-001",
            title="No Organization or equivalent identity node is declared anywhere in the sample",
            category="entity_identity",
            stage="unambiguous",
            evidence=[
                f"0 of {total} sampled page(s) contain a JSON-LD node typed as "
                "Organization, LocalBusiness, Brand, Store or WebSite.",
                f"Total JSON-LD nodes seen across the sample: "
                f"{sum(analyses[u]['jsonld_nodes'] for u in pages)}.",
            ],
            stage_block=2, fact_criticality=3,
            scope=site_wide_scope, blast_radius=site_wide_radius,
            affected_urls=pages[:12],
            action_summary=(
                "Publish one Organization JSON-LD node on the home page carrying the "
                "exact legal and trading name, the canonical url, the logo and the "
                "founding date, and reference it by @id from other pages."
            ),
            action_rationale=(
                "Without a declared identity a system has to infer which real-world "
                "entity the site belongs to from prose alone. Where the brand name "
                "collides with another company, a product, a place or a common word, "
                "that inference goes wrong and the answer describes someone else."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm identity_nodes is at least 1.",
        ))

    # --- 2. Identity declared but not linked outward ----------------------
    elif not all_same_as:
        state["findings"].append(finding(
            local_id="FC-002",
            title="Identity node declares no sameAs links, so the brand is not tied to any external record",
            category="entity_identity",
            stage="unambiguous",
            evidence=[
                f"{len(identity_pages)} of {total} sampled page(s) declare an identity "
                "node, and none of them include a sameAs property.",
                f"Declared name(s): "
                f"{', '.join(sorted({n for u in pages for n in analyses[u]['identity_names']})) or 'none'}.",
            ],
            stage_block=2, fact_criticality=3,
            scope=site_wide_scope, blast_radius=site_wide_radius,
            affected_urls=identity_pages[:12],
            action_summary=(
                "Add a sameAs array to the Organization node listing the brand's "
                "Wikidata item, official social profiles, app store listings and any "
                "registry entry, and keep those profiles consistent with the site."
            ),
            action_rationale=(
                "sameAs is what converts a name into an identity. It gives a machine "
                "independent records to pivot on, which is both how ambiguity gets "
                "resolved and how a claim on the site becomes corroborated rather than "
                "self-reported."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm same_as is non-empty.",
        ))

    # --- 3. Conflicting names for the same entity -------------------------
    declared = {n for u in pages for n in analyses[u]["identity_names"]}
    og_names = {analyses[u]["og_site_name"] for u in pages if analyses[u]["og_site_name"]}
    combined = {n.strip().lower() for n in (declared | og_names) if n}
    if len(combined) > 1:
        state["findings"].append(finding(
            local_id="FC-003",
            title="The site states more than one name for itself",
            category="entity_identity",
            stage="unambiguous",
            evidence=[
                f"Distinct entity names found across the sample: "
                f"{', '.join(sorted(declared | og_names))}.",
                "Sources compared: JSON-LD identity node name and og:site_name.",
            ],
            stage_block=2, fact_criticality=3,
            scope=site_wide_scope, blast_radius=site_wide_radius,
            affected_urls=pages[:12],
            action_summary=(
                "Choose one canonical name string and use it identically in the "
                "Organization node, og:site_name and the title suffix. Express any "
                "trading names through alternateName rather than by varying the primary name."
            ),
            action_rationale=(
                "Agreement is what makes a fact repeatable. When a site disagrees with "
                "itself about its own name, no external source can corroborate either "
                "version, and a retrieval system has no basis to prefer one."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm a single canonical name is reported.",
        ))

    # --- 4. Undated claims -------------------------------------------------
    undated = [u for u in pages if not analyses[u]["has_date_signal"]]
    if undated:
        scope, radius = scope_from_ratio(len(undated), total)
        state["findings"].append(finding(
            local_id="FC-004",
            title="Content carries no date signal, so a fresh claim cannot outrank a stale one",
            category="freshness",
            stage="current",
            evidence=[
                f"{len(undated)} of {total} sampled page(s) expose no dateModified, "
                "datePublished, time element or article modified-time metadata.",
                "Example: " + undated[0],
            ],
            stage_block=2, fact_criticality=2,
            scope=scope, blast_radius=radius,
            affected_urls=undated[:12],
            action_summary=(
                "Emit dateModified on every page that carries a factual claim, and "
                "surface a visible last-updated line next to prices, specifications "
                "and policy statements."
            ),
            action_rationale=(
                "Updating a page does not delete the older version of the fact from "
                "the rest of the web. When the current statement is undated and the "
                "stale one is dated, recency cannot be used to prefer the current one, "
                "so the outdated version keeps winning."
            ),
            effort="medium",
            verify_by="Re-run this audit and confirm has_date_signal on content pages.",
        ))

    return all_same_as[:MAX_SAMEAS_CHECKS]


class _BoundedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, max_redirects: int) -> None:
        super().__init__()
        self.max_redirects = max_redirects
        self.redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.redirects >= self.max_redirects:
            raise URLError("maximum redirect limit reached")
        self.redirects += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def bounded_get(url: str, user_agent: str, timeout: float) -> dict:
    """Bounded public GET used only for the explicit sameAs corroboration exception."""
    handler = _BoundedRedirectHandler(MAX_REDIRECTS)
    context = ssl.create_default_context()
    from urllib.request import HTTPSHandler
    opener = build_opener(handler, HTTPSHandler(context=context))
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=max(0.1, min(timeout, DEFAULT_TIMEOUT_SECONDS))) as response:
            response.read(MAX_SAMEAS_BYTES)
            return {
                "outcome": "retrieved",
                "http_status": getattr(response, "status", None),
            }
    except HTTPError as exc:
        return {
            "outcome": "unavailable" if exc.code in (404, 410) else "error",
            "http_status": exc.code,
        }
    except Exception as exc:
        return {"outcome": "error", "http_status": None, "error": str(exc)}


def verify_same_as(targets, user_agent, timeout, state) -> None:
    """
    Bounded corroboration probe.

    A declared sameAs link only corroborates the identity if the target actually
    exists. This checks existence, not content quality, and reports at medium
    confidence because a single environment can be blocked where a user is not.
    """
    if not targets:
        return

    unreachable = []
    checked = []
    for index, target in enumerate(targets):
        if index:
            time.sleep(POLITE_DELAY_SECONDS)
        result = bounded_get(target, user_agent, timeout)
        checked.append({
            "url": target,
            "outcome": result.get("outcome"),
            "status": result.get("http_status"),
        })
        if result.get("outcome") == "unavailable" and result.get("http_status") in (404, 410):
            unreachable.append(target)

    state["observations"].append(
        "sameAs verification: " + json.dumps(checked, sort_keys=True)
    )
    if unreachable:
        scope, radius = site_scope(len(targets))
        state["findings"].append(finding(
            local_id="FC-005",
            title="Declared sameAs profiles do not resolve",
            category="corroboration",
            stage="corroborated",
            evidence=[
                f"{len(unreachable)} of {len(targets)} checked sameAs target(s) "
                "returned 404 or 410.",
                "Unresolved: " + ", ".join(sorted(unreachable)),
            ],
            stage_block=2, fact_criticality=2,
            scope=scope, blast_radius=radius,
            affected_urls=sorted(unreachable),
            action_summary=(
                "Correct or remove the dead sameAs entries and point them at profiles "
                "that exist and name the same entity."
            ),
            action_rationale=(
                "A sameAs link that leads nowhere corroborates nothing and weakens the "
                "credibility of the identity block it sits in. A short accurate list "
                "is worth more than a long aspirational one."
            ),
            effort="low",
            verify_by="Re-run this audit and confirm every checked sameAs target resolves.",
            confidence="medium",
        ))

    state["limitations"].append({
        "check": "external_corroboration",
        "reason": (
            f"Only {len(targets)} declared sameAs target(s) were checked, and only for "
            "existence. Whether independent third-party sources agree with the site's "
            "claims was not assessed; that requires querying the open web, which this "
            "marketplace deliberately does not do."
        ),
        "affected_checks": ["third_party_agreement", "contradiction_detection"],
    })


def proactive() -> list[dict]:
    return [
        {
            "title": "Publish a dated, machine-readable canonical facts page",
            "summary": (
                "Create one page holding the brand's current identity and commercial "
                "facts as plain declarative sentences with a visible last-updated date "
                "and matching JSON-LD, and link it from the footer of every page."
            ),
            "rationale": (
                "A single quotable, dated, unambiguous source is the cheapest thing a "
                "brand can offer a retrieval system. It gives every assistant the same "
                "answer to pick up, which is how consistency across the web starts."
            ),
            "priority": "high",
            "category": "corroboration",
        },
        {
            "title": "Publish explicit supersession statements when facts change",
            "summary": (
                "When a name, tagline, logo, price or product is retired, publish a "
                "dated sentence that names both the old value and the new one, for "
                "example that the current tagline replaced the previous one in a given "
                "month, and keep those statements in a durable changelog."
            ),
            "rationale": (
                "Publishing the new fact does not remove the old one from the rest of "
                "the web, and the old version usually has more copies. A supersession "
                "statement is the only thing that lets a system encountering the stale "
                "version discover that it has been replaced rather than treating it as "
                "an equally valid competing claim."
            ),
            "priority": "high",
            "category": "freshness",
        },
        {
            "title": "Make third-party descriptions match the site's own",
            "summary": (
                "Audit how retailers, directories, marketplaces and app stores describe "
                "the brand, and align those descriptions with the canonical facts page "
                "so the same sentence appears in many independent places."
            ),
            "rationale": (
                "A claim that lives in one place is fragile. The same claim repeated "
                "consistently across unrelated sources is what a machine treats as "
                "settled, and it is also what stops someone else's wording from "
                "becoming the brand's de facto description."
            ),
            "priority": "medium",
            "category": "corroboration",
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

        # Freshness/identity analysis now consumes normalized evidence, but the
        # presence of HTML remains a useful collection guard because the contract
        # explicitly treats missing page evidence as a limitation, not a defect.
        html = page.get("html")
        has_html = (
            isinstance(html, dict) and isinstance(html.get("content"), str)
        ) or isinstance(html, str)
        if error or (status is not None and not (200 <= int(status) < 300)):
            failed += 1
            continue
        if not has_html:
            missing_html += 1
            continue

        analyses[url] = analyze_identity(url, page)

    state["observations"].append(
        f"Analysed {len(analyses)} of {len(valid_pages)} sampled page(s) from shared evidence."
    )
    if failed:
        state["limitations"].append({
            "check": "page_retrieval",
            "reason": (
                f"{failed} sampled page(s) have retrieval errors or non-2xx status in "
                "evidence.json; identity and freshness could not be assessed for them."
            ),
            "affected_checks": ["entity_identity", "freshness"],
        })
        state["status"] = "partial_failure"
    if missing_html:
        state["limitations"].append({
            "check": "page_evidence",
            "reason": (
                f"{missing_html} sampled page(s) have no usable html evidence in "
                "evidence.json; identity/freshness checks were not run for those pages."
            ),
            "affected_checks": ["entity_identity", "freshness"],
        })
        state["status"] = "partial_failure"

    return analyses


def run(url, evidence_file, user_agent, timeout, no_external) -> dict:
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
            "reason": "Shared evidence contains no sampled pages; no identity/freshness checks were run.",
            "affected_checks": ["all_identity_and_freshness_checks"],
        })
        return finalize(state)

    parts = urlsplit(str(evidence.get("site") or url))
    origin = f"{parts.scheme or 'https'}://{parts.netloc}"
    analyses = audit_pages(evidence, state)
    if not analyses:
        return finalize(state)

    targets = evaluate(analyses, origin, state)
    if not no_external:
        verify_same_as(targets, user_agent, timeout, state)
    else:
        state["limitations"].append({
            "check": "external_corroboration",
            "reason": "External sameAs verification was disabled for this run.",
            "affected_checks": ["third_party_agreement"],
        })

    state["proactive_recommendations"] = proactive()
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
        description="Audit entity identity, freshness signals and corroboration."
    )
    parser.add_argument("url")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--budget-ms", type=int, default=DEFAULT_BUDGET_MS)
    parser.add_argument("--evidence-file", default=None)
    parser.add_argument("--sample-file", default=None)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument(
        "--no-external",
        action="store_true",
        help="Skip verification of declared sameAs targets.",
    )
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
        result = run(
            args.url.strip(),
            args.evidence_file,
            args.user_agent,
            args.timeout,
            args.no_external,
        )
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
