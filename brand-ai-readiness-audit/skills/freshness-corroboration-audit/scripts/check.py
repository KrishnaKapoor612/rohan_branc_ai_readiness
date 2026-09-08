#!/usr/bin/env python3
"""
check.py -- specialist coordinator for the freshness-corroboration-audit skill.

Owns the trust half of discoverability. A page can be perfectly reachable,
rendered and extractable and still lose, because the assistant cannot tell which
entity it describes, cannot tell how old the claim is, and finds the same claim
contradicted elsewhere on the open web.

Three distinct failure modes, all downstream of extraction:

    unambiguous  - is it clear which real-world entity this is?
    corroborated - does anything outside this site agree?
    current      - is there any signal about when this was true?

This skill does NOT own crawl permission, rendering, extraction structure
(crawl-render-audit) or post-arrival orientation (engagement-audit).

Read-only and bounded. The only requests outside the target origin are HEAD-like
GETs of the sameAs profile URLs the site itself declares, capped at
MAX_SAMEAS_CHECKS, which is the corroboration check the contract's section 8
explicitly permits.
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

IDENTITY_TYPES = {
    "organization", "corporation", "localbusiness", "onlinestore",
    "store", "brand", "website", "ngo", "educationalorganization",
}

DATE_FIELDS = ("dateModified", "datePublished", "dateCreated", "uploadDate")

DATE_MARKUP = re.compile(
    r'(<time\b[^>]*datetime\s*=|itemprop\s*=\s*["\']date(Modified|Published)["\']'
    r'|property\s*=\s*["\']article:(modified|published)_time["\'])',
    re.I,
)

OG_SITE_NAME = re.compile(
    r'<meta[^>]+property\s*=\s*["\']og:site_name["\'][^>]+content\s*=\s*["\'](.*?)["\']',
    re.I | re.S,
)


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

def load_sibling(module_name: str) -> tuple[Optional[Any], Optional[str]]:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, f"{module_name}.py")
    if not os.path.isfile(path):
        return None, f"{module_name}.py is not present at {path}."
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None, f"{module_name}.py could not be loaded as a module."
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module, None
    except Exception as exc:
        return None, f"{module_name}.py failed to import: {type(exc).__name__}: {exc}"


def load_sample(path: Optional[str], fallback_url: str) -> tuple[list[str], Optional[str]]:
    if not path:
        return [fallback_url], (
            "No shared page sample was supplied; only the target URL was audited, "
            "so site-wide scope could not be established."
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            sample = json.load(handle)
        urls = [u for u in (sample.get("urls") or []) if isinstance(u, str)]
        if not urls:
            return [fallback_url], (
                f"Shared page sample at {path} contained no URLs."
            )
        return urls, None
    except Exception as exc:
        return [fallback_url], (
            f"Shared page sample at {path} could not be read "
            f"({type(exc).__name__}: {exc})."
        )


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._depth > 0:
            self._depth -= 1

    def handle_data(self, data):
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


def flatten_jsonld(html: str) -> list[dict]:
    """Every JSON-LD node on the page, graphs expanded, parse failures skipped."""
    nodes: list[dict] = []
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    )
    for block in blocks:
        try:
            parsed = json.loads(block.strip())
        except Exception:
            continue
        stack = parsed if isinstance(parsed, list) else [parsed]
        while stack:
            node = stack.pop(0)
            if not isinstance(node, dict):
                continue
            nodes.append(node)
            graph = node.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
    return nodes


def node_types(node: dict) -> list[str]:
    value = node.get("@type")
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, list):
        return [v.lower() for v in value if isinstance(v, str)]
    return []


def analyze_identity(url: str, html: str) -> dict:
    nodes = flatten_jsonld(html)
    identity = [n for n in nodes if set(node_types(n)) & IDENTITY_TYPES]

    names: list[str] = []
    same_as: list[str] = []
    for node in identity:
        name = node.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
        raw = node.get("sameAs")
        if isinstance(raw, str):
            same_as.append(raw)
        elif isinstance(raw, list):
            same_as.extend(v for v in raw if isinstance(v, str))

    og = OG_SITE_NAME.search(html)

    dated = any(
        any(field in node for field in DATE_FIELDS) for node in nodes
    ) or bool(DATE_MARKUP.search(html))

    return {
        "url": url,
        "jsonld_nodes": len(nodes),
        "identity_nodes": len(identity),
        "identity_names": sorted(set(names)),
        "same_as": sorted(set(same_as)),
        "og_site_name": og.group(1).strip() if og else None,
        "has_date_signal": dated,
        "text_chars": len(visible_text(html)),
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
    """
    Scope for a claim about the whole origin, bounded by how much was sampled.

    severity-model.md section 4 forbids inferring site_wide from one page. A
    site-level claim backed by a single page is a single-page claim, and its
    blast_radius must say so.
    """
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


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

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
            stage_block=2,
            fact_criticality=3,
            scope=site_wide_scope,
            blast_radius=site_wide_radius,
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
            stage_block=2,
            fact_criticality=3,
            scope=site_wide_scope,
            blast_radius=site_wide_radius,
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
            stage_block=2,
            fact_criticality=3,
            scope=site_wide_scope,
            blast_radius=site_wide_radius,
            affected_urls=pages[:12],
            action_summary=(
                "Choose one canonical name string and use it identically in the "
                "Organization node, og:site_name and the title suffix. Express any "
                "trading names through alternateName rather than by varying the "
                "primary name."
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
            stage_block=2,
            fact_criticality=2,
            scope=scope,
            blast_radius=radius,
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


def verify_same_as(targets, fetch, brand_names, timeout, state) -> None:
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
        result = fetch.fetch_page(target, timeout=timeout, max_bytes=200 * 1024)
        checked.append({
            "url": target,
            "outcome": result.get("outcome"),
            "status": result.get("http_status"),
        })
        if result.get("outcome") == "unavailable" and (result.get("http_status") or 0) in (404, 410):
            unreachable.append(target)

    state["observations"].append(
        "sameAs verification: " + json.dumps(checked, sort_keys=True)
    )

    if unreachable:
        # Bounded by what was actually probed, never by the size of the sample.
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
            stage_block=2,
            fact_criticality=2,
            scope=scope,
            blast_radius=radius,
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


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def audit_pages(urls, fetch, user_agent, timeout, state) -> dict:
    analyses: dict[str, dict] = {}
    failed = 0

    for index, url in enumerate(sorted(set(urls))):
        if index:
            time.sleep(POLITE_DELAY_SECONDS)
        result = fetch.fetch_page(url, user_agent=user_agent, timeout=timeout)
        if result.get("outcome") != "retrieved":
            failed += 1
            continue
        analyses[url] = analyze_identity(url, result.get("body") or "")

    state["observations"].append(
        f"Analysed {len(analyses)} of {len(set(urls))} sampled page(s)."
    )

    if failed:
        state["limitations"].append({
            "check": "page_retrieval",
            "reason": (
                f"{failed} sampled page(s) could not be retrieved from this audit "
                "environment; identity and freshness could not be assessed for them."
            ),
            "affected_checks": ["entity_identity", "freshness"],
        })
        state["status"] = "partial_failure"

    return analyses


def run(url, user_agent, timeout, sample_file, check_external) -> dict:
    state: dict = {
        "status": "success",
        "observations": [],
        "findings": [],
        "limitations": [],
        "proactive_recommendations": [],
    }

    sample_urls, sample_limitation = load_sample(sample_file, url)
    if sample_limitation:
        state["limitations"].append({
            "check": "page_sample",
            "reason": sample_limitation,
            "affected_checks": ["scope_claims"],
        })
    state["observations"].append(f"Shared page sample: {len(sample_urls)} URL(s).")

    fetch, fetch_error = load_sibling("fetch")
    if fetch is None:
        state["limitations"].append({
            "check": "page_retrieval",
            "reason": fetch_error or "fetch.py is unavailable.",
            "affected_checks": ["all_identity_and_freshness_checks"],
        })
        state["status"] = "partial_failure"
        return finalize(state)

    parts = urlsplit(url)
    origin = f"{parts.scheme or 'https'}://{parts.netloc}"

    analyses = audit_pages(sample_urls, fetch, user_agent, timeout, state)
    targets = evaluate(analyses, origin, state)

    if check_external:
        brand_names = sorted({n for u in analyses for n in analyses[u]["identity_names"]})
        verify_same_as(targets, fetch, brand_names, timeout, state)
    else:
        state["limitations"].append({
            "check": "external_corroboration",
            "reason": "External sameAs verification was disabled for this run.",
            "affected_checks": ["third_party_agreement"],
        })

    if analyses:
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
        }, indent=2))
        return 2

    started = time.monotonic()

    try:
        result = run(args.url.strip(), args.user_agent, args.timeout,
                     args.sample_file, not args.no_external)
    except Exception as exc:  # pragma: no cover - fault isolation boundary
        result = {
            "skill": SKILL_ID, "status": "failure", "observations": [],
            "findings": [], "limitations": [{
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
