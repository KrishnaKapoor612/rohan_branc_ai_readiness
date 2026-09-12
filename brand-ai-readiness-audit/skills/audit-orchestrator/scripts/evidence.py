#!/usr/bin/env python3
"""
evidence.py -- shared evidence collector and normalizer for audit-orchestrator.

Collects reliable observations about a target website ONCE and writes them to
a canonical evidence.json that every specialist skill then analyzes. This is
the fix for the previous architecture, in which crawl-render-audit,
freshness-corroboration-audit and engagement-audit each independently fetched
the same pages.

    Collector collects. Specialist reasons.

This module does NOT:
    - decide whether an observation is a finding;
    - assign severity or funnel stage;
    - contain any specialist-specific business logic;
    - guess at information it could not actually retrieve.

Dependency direction (see references/specialist-contract.md section 7 for the
sibling-module interfaces this relies on):

    evidence.py -> robots.py, sitemap.py, fetch.py, render.py

evidence.py never imports a specialist's check.py, and no specialist imports
evidence.py. They communicate only through evidence.json.

Sibling modules are optional at run time. A missing sibling becomes a
collection limitation, never a crash and never invented evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import uuid
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

SCHEMA_VERSION = "1.0"

DEFAULT_USER_AGENT = (
    "BrandAIReadinessAuditBot/1.0 "
    "(+https://github.com/KrishnaKapoor612/rohan_branc_ai_readiness)"
)

DEFAULT_SAMPLE_LIMIT = 12
DEFAULT_TIMEOUT_SECONDS = 10.0

# Bounds. Round-3 runtime expectation is under five minutes for a typical
# site, and the evidence file itself must stay a practical size.
MAX_HTML_BYTES_PER_PAGE = 300_000          # raw HTML stored per page, truncated beyond this
MAX_FETCH_BYTES_PER_PAGE = 2 * 1024 * 1024  # bytes actually requested from the network
MAX_RENDER_PAGES = 3                        # rendering is the slow path; bound it hard
MAX_SAME_AS_CHECKS = 3                      # explicit bound from the problem statement
MAX_LINKS_RECORDED = 40
MAX_HEADINGS_RECORDED = 30

IDENTITY_JSONLD_TYPES = {
    "organization", "corporation", "localbusiness", "onlinestore", "store",
    "brand", "website", "ngo", "educationalorganization", "person",
}

DATE_JSONLD_KEYS = (
    "dateModified", "datePublished", "dateCreated", "uploadDate",
)

SEARCH_UI_HINTS = re.compile(
    r'type=["\']search["\']|name=["\'](?:q|query|search)["\']|role=["\']search["\']',
    re.IGNORECASE,
)
 
NOINDEX_META = re.compile(
    r'<meta[^>]+name=["\'](?:robots|googlebot|bingbot)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
 
 
def page_excluded_from_indexing(html_text: str, x_robots_tag: Optional[str]) -> bool:
    """
   True if the page carries a noindex directive via <meta name="robots"|
    "googlebot"|"bingbot"> or the X-Robots-Tag response header. This is a
    page-level exclusion signal, independent of robots.txt, and must be
    respected the same way: analyzed content must not be reported as a
   citable/engaging page if the site has explicitly excluded it from
    indexing.
    """
    if x_robots_tag and "noindex" in x_robots_tag.lower():
        return True
    for match in NOINDEX_META.finditer(html_text):
        if "noindex" in match.group(1).lower():
            return True
    return False

 
 # ---------------------------------------------------------------------------
 # Sibling module loading (same pattern used throughout this marketplace)


# ---------------------------------------------------------------------------
# Sibling module loading (same pattern used throughout this marketplace)
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


# ---------------------------------------------------------------------------
# Minimal HTML text extraction (stdlib only, mirrors crawl-render-audit's)
# ---------------------------------------------------------------------------

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
        if self._depth == 0:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)

    def text(self) -> str:
        return " ".join(self.chunks)


def visible_text(html_text: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html_text)
    return parser.text()


# ---------------------------------------------------------------------------
# Signal extraction -- observations only, never a conclusion
# ---------------------------------------------------------------------------

def extract_jsonld(html_text: str) -> list[dict]:
    """
    Every ld+json block, with its parse outcome preserved. An unparseable
    block is recorded as such, not silently dropped -- a specialist may care
    that structured data exists but is broken, which is different from it
    being absent.
    """
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.IGNORECASE | re.DOTALL,
    )
    results = []
    for raw in blocks:
        entry: dict = {"parsed": False, "types": [], "same_as": [], "names": []}
        try:
            parsed = json.loads(raw.strip())
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)
            continue

        entry["parsed"] = True
        nodes = parsed if isinstance(parsed, list) else [parsed]
        # A top-level @graph is common; flatten one level of it.
        flattened = []
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                flattened.extend(n for n in node["@graph"] if isinstance(n, dict))
            elif isinstance(node, dict):
                flattened.append(node)

        for node in flattened:
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            for t in types:
                if isinstance(t, str):
                    entry["types"].append(t)

            name = node.get("name")
            if isinstance(name, str) and any(
                isinstance(t, str) and t.lower() in IDENTITY_JSONLD_TYPES for t in types
            ):
                entry["names"].append(name)

            same_as = node.get("sameAs")
            if isinstance(same_as, str):
                entry["same_as"].append(same_as)
            elif isinstance(same_as, list):
                entry["same_as"].extend(s for s in same_as if isinstance(s, str))

            for key in DATE_JSONLD_KEYS:
                if isinstance(node.get(key), str):
                    entry.setdefault("dates", []).append({"field": key, "value": node[key]})

        entry["types"] = sorted(set(entry["types"]))
        entry["same_as"] = sorted(set(entry["same_as"]))
        results.append(entry)

    return results


def extract_open_graph(html_text: str) -> dict:
    tags = re.findall(
        r'<meta[^>]+property=["\'](og:[^"\']+)["\'][^>]+content=["\'](.*?)["\']',
        html_text, re.IGNORECASE | re.DOTALL,
    )
    og: dict = {}
    for prop, content in tags:
        og.setdefault(prop.lower(), content.strip())
    return og


def extract_title_and_description(html_text: str) -> tuple[Optional[str], Optional[str]]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else None

    desc_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html_text, re.IGNORECASE | re.DOTALL,
    )
    description = desc_match.group(1).strip() if desc_match else None
    return title, description


def extract_canonical(html_text: str) -> Optional[str]:
    match = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](.*?)["\']',
        html_text, re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def extract_headings(html_text: str) -> list[dict]:
    headings = []
    for match in re.finditer(
        r"<h([1-3])\b([^>]*)>(.*?)</h\1>", html_text, re.IGNORECASE | re.DOTALL,
    ):
        level, attrs, inner = match.groups()
        has_id = bool(re.search(r'\bid\s*=\s*["\'][^"\']+["\']', attrs, re.IGNORECASE))
        text = re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        headings.append({"level": int(level), "text": text[:200], "has_id": has_id})
        if len(headings) >= MAX_HEADINGS_RECORDED:
            break
    return headings


def extract_links(html_text: str, base_url: str) -> dict:
    origin = urlsplit(base_url).netloc
    hrefs = re.findall(r'<a\b[^>]+href=["\'](.*?)["\']', html_text, re.IGNORECASE | re.DOTALL)

    internal = 0
    external = 0
    sample = []
    for href in hrefs:
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        is_internal = urlsplit(absolute).netloc == origin
        if is_internal:
            internal += 1
        else:
            external += 1
        if len(sample) < MAX_LINKS_RECORDED:
            sample.append({"href": absolute, "internal": is_internal})

    return {"internal_count": internal, "external_count": external, "sample": sample}


def extract_date_signals(html_text: str, jsonld: list[dict]) -> list[dict]:
    signals = []

    for match in re.finditer(
        r'<time\b[^>]*\bdatetime=["\'](.*?)["\']', html_text, re.IGNORECASE,
    ):
        signals.append({"source": "time_datetime", "value": match.group(1).strip()})

    for meta_name in ("article:modified_time", "article:published_time", "og:updated_time"):
        match = re.search(
            rf'<meta[^>]+property=["\']{re.escape(meta_name)}["\'][^>]+content=["\'](.*?)["\']',
            html_text, re.IGNORECASE,
        )
        if match:
            signals.append({"source": meta_name, "value": match.group(1).strip()})

    for block in jsonld:
        for date in block.get("dates", []):
            signals.append({"source": f"jsonld_{date['field']}", "value": date["value"]})

    return signals


def extract_identity_signals(
    title: Optional[str], og: dict, jsonld: list[dict], canonical: Optional[str],
) -> list[dict]:
    signals = []
    if title:
        signals.append({"source": "title", "value": title})
    if og.get("og:site_name"):
        signals.append({"source": "og:site_name", "value": og["og:site_name"]})
    for block in jsonld:
        for name in block.get("names", []):
            signals.append({"source": "jsonld_name", "value": name})
    if canonical:
        signals.append({"source": "canonical", "value": canonical})
    return signals


def extract_engagement_signals(html_text: str, headings: list[dict]) -> dict:
    form_count = len(re.findall(r"<form\b", html_text, re.IGNORECASE))
    nav_count = len(re.findall(r"<nav\b", html_text, re.IGNORECASE))
    search_ui = bool(SEARCH_UI_HINTS.search(html_text))
    headings_with_id = sum(1 for h in headings if h.get("has_id"))

    return {
        "form_count": form_count,
        "nav_element_count": nav_count,
        "search_ui_detected": search_ui,
        "headings_total": len(headings),
        "headings_with_id": headings_with_id,
    }


def extract_page_structure(html_text: str, text: str) -> dict:
    images = re.findall(r"<img\b[^>]*>", html_text, re.IGNORECASE)
    images_without_alt = [
        tag for tag in images
        if not re.search(r'\balt\s*=\s*["\'][^"\']+["\']', tag, re.IGNORECASE)
    ]
    hydration_markers = [
        m for m in (
            "__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__APOLLO_STATE__",
            "window.__data", "ng-version", "data-reactroot",
        )
        if m in html_text
    ]
    return {
        "text_chars": len(text),
        "html_bytes": len(html_text),
        "h1_count": len(re.findall(r"<h1\b", html_text, re.IGNORECASE)),
        "h2_count": len(re.findall(r"<h2\b", html_text, re.IGNORECASE)),
        "image_count": len(images),
        "images_without_alt": len(images_without_alt),
        "hydration_markers": hydration_markers,
    }


def truncate_html(html_text: str) -> dict:
    if len(html_text) <= MAX_HTML_BYTES_PER_PAGE:
        return {"content": html_text, "status": "complete"}
    return {"content": html_text[:MAX_HTML_BYTES_PER_PAGE], "status": "truncated"}


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect(
    url: str,
    limit: int,
    timeout: float,
    render_enabled: bool,
    max_render_pages: int,
    user_agent: str,
) -> dict:
    limitations: list[dict] = []

    robots, robots_err = load_sibling("robots")
    fetch, fetch_err = load_sibling("fetch")
    render, render_err = load_sibling("render")
    sitemap, sitemap_err = load_sibling("sitemap")

    for name, err in (("robots", robots_err), ("fetch", fetch_err), ("sitemap", sitemap_err)):
        if err:
            limitations.append({
                "check": f"{name}_module",
                "reason": err,
                "affected_checks": ["collection"],
            })

    if fetch is None:
        # Without fetch.py nothing can be collected. Fail closed with a
        # structurally valid, empty evidence document rather than crashing.
        return {
            "schema_version": SCHEMA_VERSION,
            "site": url,
            "collected_at": None,
            "collection": {"sample_limit": limit, "timeout_seconds": timeout,
                           "render_enabled": render_enabled},
            "pages": [],
            "site_level": {"limitations": limitations},
            "limitations": limitations + [{
                "check": "collection",
                "reason": "fetch.py is unavailable; no network collection was possible.",
                "affected_checks": ["all"],
            }],
        }

    target_url = fetch.DEFAULT_USER_AGENT and url  # normalize below if robots available
    if robots is not None:
        try:
            target_url = robots.normalize_url(url)
        except Exception as exc:
            limitations.append({
                "check": "url_normalization",
                "reason": f"Could not normalize target URL: {exc}",
                "affected_checks": ["all"],
            })
            target_url = url
    origin = f"{urlsplit(target_url).scheme}://{urlsplit(target_url).netloc}"

    # -- Robots: fetch and parse ONCE. Every later permission check reuses
    #    this parse instead of re-fetching robots.txt per page. --------------
    parsed_robots = None
    robots_fetch = None
    declared_sitemaps: list[str] = []
    site_robots_evidence: dict = {"retrieved": False}

    if robots is not None:
        robots_url = robots.robots_txt_url(target_url)
        robots_fetch = robots.fetch_robots_txt(robots_url, user_agent=user_agent, timeout=timeout)

        if robots_fetch.outcome == "ok":
            parsed_robots = robots.parse_robots_txt(robots_fetch.body or "")
            declared_sitemaps = parsed_robots.sitemaps
            group = robots.select_group(parsed_robots.groups, user_agent)
            ai_directives = robots.inspect_ai_agents(parsed_robots, "/", robots.AI_AGENT_TOKENS)
            site_robots_evidence = {
                "retrieved": True,
                "robots_url": robots_url,
                "http_status": robots_fetch.http_status,
                "sitemaps_declared": declared_sitemaps,
                "ai_agent_directives": ai_directives,
            }
        elif robots_fetch.outcome == "unavailable":
            site_robots_evidence = {
                "retrieved": False,
                "robots_url": robots_url,
                "http_status": robots_fetch.http_status,
                "reason": "robots.txt returned a 4xx status; no exclusion rules apply.",
            }
        else:
            site_robots_evidence = {
                "retrieved": False,
                "robots_url": robots_url,
                "http_status": robots_fetch.http_status,
                "reason": robots_fetch.error,
            }
            limitations.append({
                "check": "robots_policy",
                "reason": f"robots.txt could not be retrieved: {robots_fetch.error}. "
                          "Crawl permission is unverified for all sampled pages.",
                "affected_checks": ["robots_allowed"],
            })
    else:
        limitations.append({
            "check": "robots_policy",
            "reason": robots_err or "robots.py is unavailable.",
            "affected_checks": ["robots_allowed", "page_sample"],
        })

    def robots_allowed_for(path_url: str) -> Optional[bool]:
        """None means unknown (never treated as permission)."""
        if robots is None or parsed_robots is None:
            return None
        group = robots.select_group(parsed_robots.groups, user_agent)
        path = robots.target_path(path_url)
        allowed, _rule = robots.evaluate_path(group, path)
        return allowed

    # -- Sample: reuse sitemap.discover() directly, passing the sitemaps we
    #    already found so it does not re-fetch robots.txt itself. -----------
    sample_urls = [origin + "/"]
    site_sitemap_evidence: dict = {"documents_read": 0, "url_count_available": 0,
                                    "truncated": False, "strategy": "Homepage only."}

    if sitemap is not None and robots_fetch is not None and robots_fetch.outcome != "unknown":
        try:
            discovery = sitemap.discover(
                target_url, declared_sitemaps, limit=limit, timeout=timeout,
                user_agent=user_agent,
            )
            sample_urls = discovery["sample"]
            site_sitemap_evidence = {
                "documents_read": discovery["sitemap_documents_read"],
                "url_count_available": discovery["url_count"],
                "truncated": discovery["truncated"],
                "strategy": discovery["sample_strategy"],
                "sitemaps": discovery["sitemaps"],
            }
            for lim in discovery.get("limitations", []):
                limitations.append({
                    "check": lim.get("check", "sitemap_discovery"),
                    "reason": lim.get("reason", ""),
                    "affected_checks": lim.get("affected_checks", []),
                })
        except Exception as exc:
            limitations.append({
                "check": "sitemap_discovery",
                "reason": f"Sitemap discovery failed: {type(exc).__name__}: {exc}. "
                          "Falling back to the homepage only.",
                "affected_checks": ["page_sample"],
            })
    elif sitemap is None:
        limitations.append({
            "check": "sitemap_discovery",
            "reason": sitemap_err or "sitemap.py is unavailable.",
            "affected_checks": ["page_sample"],
        })
    elif robots_fetch is not None and robots_fetch.outcome == "unknown":
        limitations.append({
            "check": "sitemap_discovery",
            "reason": "robots.txt permission is unknown; sitemap discovery and "
                      "sampling beyond the homepage were skipped rather than "
                      "assumed permitted.",
            "affected_checks": ["page_sample"],
        })

    # -- Per-page collection --------------------------------------------------
    pages: list[dict] = []
    same_as_candidates: set[str] = set()
    rendered_so_far = 0

    for index, page_url in enumerate(sample_urls):
        page: dict = {"url": page_url, "final_url": None, "status": None,
                       "content_type": None, "robots_allowed": None,
                       "error": None, "limitations": []}

        allowed = robots_allowed_for(page_url)
        page["robots_allowed"] = allowed

        if allowed is False:
            page["limitations"].append(
                "Page evidence unavailable because robots.txt disallows this path."
            )
            pages.append(page)
            continue

        if allowed is None:
            page["limitations"].append(
                "Crawl permission for this path could not be verified; "
                "the page was not fetched."
            )
            pages.append(page)
            continue

        result = fetch.fetch_page(page_url, user_agent=user_agent, timeout=timeout,
                                  max_bytes=MAX_FETCH_BYTES_PER_PAGE)
        page["final_url"] = result.get("final_url")
        page["status"] = result.get("http_status")
        page["content_type"] = result.get("content_type")

        if result.get("outcome") != "retrieved" or not result.get("body"):
            page["error"] = {
                "type": result.get("outcome"),
                "message": result.get("error") or f"HTTP {result.get('http_status')}",
            }
            page["limitations"].append(
                f"Page evidence unavailable: outcome={result.get('outcome')}."
            )
            pages.append(page)
            continue

        body = result["body"]
        excluded = page_excluded_from_indexing(body, result.get("x_robots_tag"))
        page["indexable"] = not excluded
        if excluded:
            page["limitations"].append(
                "Page carries a noindex directive (meta robots or X-Robots-Tag); "
                "retrieved for permission-checking only and excluded from "
                "content-based analysis, per the marketplace's obligation to "
                "respect page-level indexing directives, not just robots.txt."
            )
            pages.append(page)
            continue
        text = visible_text(body)
        jsonld = extract_jsonld(body)
        og = extract_open_graph(body)
        title, description = extract_title_and_description(body)
        canonical = extract_canonical(body)
        headings = extract_headings(body)

        for block in jsonld:
            same_as_candidates.update(block.get("same_as", []))

        page.update({
            "html": truncate_html(body),
            "title": title,
            "meta_description": description,
            "canonical": canonical,
            "jsonld": jsonld,
            "open_graph": og,
            "headings": headings,
            "links": extract_links(body, page_url),
            "date_signals": extract_date_signals(body, jsonld),
            "identity_signals": extract_identity_signals(title, og, jsonld, canonical),
            "same_as": sorted({s for b in jsonld for s in b.get("same_as", [])}),
            "engagement_signals": extract_engagement_signals(body, headings),
            "page_structure": extract_page_structure(body, text),
            "render": {"attempted": False, "available": False, "signals": {}},
        })
        
        if page["html"]["status"] == "truncated":
           page["limitations"].append(
               "Stored HTML evidence was truncated at "
               f"{MAX_HTML_BYTES_PER_PAGE} bytes. "
               "Absence-based conclusions from the stored HTML may be incomplete."
      )

        if render_enabled and render is not None and rendered_so_far < max_render_pages:
            rendered_so_far += 1
            render_result = render.render_page(page_url, timeout=timeout)
            if render_result.get("available") and render_result.get("outcome") == "rendered":
                rendered_text = visible_text(render_result.get("html") or "")
                page["render"] = {
                    "attempted": True,
                    "available": True,
                    "backend": render_result.get("backend"),
                    "signals": {
                        "text_chars": len(rendered_text),
                        "server_text_chars": page["page_structure"]["text_chars"],
                        "gained_chars": len(rendered_text) - page["page_structure"]["text_chars"],
                    },
                }
            else:
                page["render"] = {
                    "attempted": True,
                    "available": False,
                    "error": render_result.get("error") or render_result.get("outcome"),
                    "signals": {},
                }
        elif render_enabled and render is None:
            page["limitations"].append(
                render_err or "render.py is unavailable; rendered evidence was not collected."
            )

        pages.append(page)

    # -- Bounded, explicit sameAs verification (max 3, deterministic order) --
    same_as_checks = []
    for candidate in sorted(same_as_candidates)[:MAX_SAME_AS_CHECKS]:
        result = fetch.fetch_page(candidate, user_agent=user_agent, timeout=timeout,
                                  max_bytes=1024)
        same_as_checks.append({
            "url": candidate,
            "outcome": result.get("outcome"),
            "http_status": result.get("http_status"),
        })

    site_level = {
        "origin": origin,
        "robots": site_robots_evidence,
        "sitemap": site_sitemap_evidence,
        "corroboration": {
            "same_as_candidates_found": len(same_as_candidates),
            "same_as_checked": same_as_checks,
            "bound": MAX_SAME_AS_CHECKS,
        },
        "limitations": limitations,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "site": target_url,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collection": {
            "sample_limit": limit,
            "timeout_seconds": timeout,
            "render_enabled": render_enabled,
            "max_render_pages": max_render_pages,
            "user_agent": user_agent,
        },
        "pages": pages,
        "site_level": site_level,
        "limitations": limitations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect and normalize shared audit evidence for a target site."
    )
    parser.add_argument("url")
    parser.add_argument("--limit", type=int, default=DEFAULT_SAMPLE_LIMIT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--max-render-pages", type=int, default=MAX_RENDER_PAGES)

    args = parser.parse_args()

    work_dir = args.work_dir or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"brand-ai-audit-{uuid.uuid4().hex[:12]}"
    )
    os.makedirs(work_dir, exist_ok=True)

    evidence = collect(
        url=args.url,
        limit=args.limit,
        timeout=args.timeout,
        render_enabled=not args.no_render,
        max_render_pages=args.max_render_pages,
        user_agent=args.user_agent,
    )

    evidence_file = os.path.join(work_dir, "evidence.json")
    with open(evidence_file, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2, ensure_ascii=False)

    # Responsibility #17: return the path to dispatch.py. dispatch.py invokes
    # this as a subprocess and reads evidence_file from stdout, exactly as it
    # already reads sample_file from sitemap.py --emit-sample.
    print(json.dumps({
        "evidence_file": evidence_file,
        "site": evidence["site"],
        "pages_collected": sum(1 for p in evidence["pages"] if p.get("status")),
        "pages_sampled": len(evidence["pages"]),
        "limitations": len(evidence["limitations"]),
    }, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
