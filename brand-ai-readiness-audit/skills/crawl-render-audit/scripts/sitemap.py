#!/usr/bin/env python3
"""
sitemap.py -- sitemap discovery and deterministic page sampling.

Its primary job is not to confirm that a sitemap exists. It is to produce the
shared page sample that every specialist in the marketplace audits.

If each specialist chose its own pages, findings would not be comparable across
skills and two runs of the same audit could disagree. One deterministic sample,
built once and passed to everyone, is what makes the report reproducible and
makes a scope claim like "0 of 12 product pages" meaningful.

This module does NOT:
    - crawl an unbounded number of URLs;
    - treat a missing sitemap as a defect;
    - follow links outside the target origin;
    - fetch anything robots.txt disallows.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

DEFAULT_SAMPLE_LIMIT = 12
MAX_SITEMAP_DOCUMENTS = 5
MAX_URLS_SCANNED = 5000
MAX_SITEMAP_BYTES = 10 * 1024 * 1024
POLITE_DELAY_SECONDS = 0.5

# Path patterns ranked by how much a machine reader's answer depends on them.
# Lower rank sorts earlier. Patterns are generic on purpose: the marketplace is
# graded on unseen sites, so nothing here may encode a specific brand or CMS.
PATH_PRIORITIES: list[tuple[int, str, re.Pattern]] = [
    (0, "home", re.compile(r"^/?$")),
    (1, "product", re.compile(r"/(product|products|item|items|shop|store|p)/", re.I)),
    (2, "pricing", re.compile(r"/(pricing|plans|price|subscribe)", re.I)),
    (3, "category", re.compile(r"/(collection|collections|category|categories|c)/", re.I)),
    (4, "about", re.compile(r"/(about|company|who-we-are|our-story|brand)", re.I)),
    (5, "contact", re.compile(r"/(contact|support|help|stores|locations|find-us)", re.I)),
    (6, "service", re.compile(r"/(services|solutions|features|platform)", re.I)),
    (7, "content", re.compile(r"/(blog|news|press|articles|resources|insights)", re.I)),
]
DEFAULT_RANK = 8


def load_sibling(module_name: str) -> tuple[Optional[object], Optional[str]]:
    """Import a sibling script. See references/specialist-contract.md section 7."""
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


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme or "https"
    return f"{scheme}://{parts.netloc}"


def same_origin(candidate: str, origin: str) -> bool:
    return urlsplit(candidate).netloc == urlsplit(origin).netloc


def classify(url: str) -> tuple[int, str]:
    """Return (rank, label) for a URL path. Deterministic, first match wins."""
    path = urlsplit(url).path or "/"
    for rank, label, pattern in PATH_PRIORITIES:
        if pattern.search(path):
            return rank, label
    return DEFAULT_RANK, "other"


def parse_sitemap_xml(text: str) -> dict:
    """
    Parse a urlset or sitemapindex document.

    Returns {"kind": "urlset"|"sitemapindex"|"unknown", "entries": [...]}.
    Malformed XML yields an empty result rather than raising, because a broken
    sitemap is an observation for the caller, not a crash.
    """
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        return {"kind": "unknown", "entries": [], "error": f"XML parse error: {exc}"}

    tag = root.tag.split("}")[-1].lower()
    entries: list[str] = []

    for child in root:
        for leaf in child:
            if leaf.tag.split("}")[-1].lower() == "loc" and leaf.text:
                entries.append(leaf.text.strip())
                break

    if tag == "sitemapindex":
        return {"kind": "sitemapindex", "entries": entries}
    if tag == "urlset":
        return {"kind": "urlset", "entries": entries}
    return {"kind": "unknown", "entries": entries}


def discover(
    url: str,
    declared_sitemaps: Optional[list[str]] = None,
    limit: int = DEFAULT_SAMPLE_LIMIT,
    timeout: float = 10.0,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Discover sitemap documents and build the shared page sample.

    Order of preference for sitemap locations:
        1. Sitemap: lines declared in robots.txt
        2. The conventional /sitemap.xml

    Bounded at MAX_SITEMAP_DOCUMENTS documents and MAX_URLS_SCANNED URLs.
    """
    fetch, fetch_error = load_sibling("fetch")

    origin = origin_of(url)
    homepage = origin + "/"

    limitations: list[dict] = []
    observations: list[str] = []

    if fetch is None:
        limitations.append({
            "check": "sitemap_discovery",
            "reason": fetch_error or "fetch.py is unavailable.",
            "affected_checks": ["page_sample"],
        })
        return {
            "origin": origin,
            "sitemaps": [],
            "sitemap_documents_read": 0,
            "url_count": 0,
            "truncated": False,
            "sample": [homepage],
            "sample_strategy": "Homepage only: sitemap discovery was unavailable.",
            "observations": observations,
            "limitations": limitations,
        }

    agent = user_agent or fetch.DEFAULT_USER_AGENT

    candidates: list[str] = []
    for candidate in (declared_sitemaps or []):
        absolute = urljoin(origin + "/", candidate)
        if same_origin(absolute, origin) and absolute not in candidates:
            candidates.append(absolute)

    conventional = urljoin(origin + "/", "/sitemap.xml")
    if conventional not in candidates:
        candidates.append(conventional)

    collected: list[str] = []
    documents_read = 0
    queue = list(candidates)
    seen_documents: set[str] = set()
    truncated = False

    while queue and documents_read < MAX_SITEMAP_DOCUMENTS:
        document_url = queue.pop(0)
        if document_url in seen_documents:
            continue
        seen_documents.add(document_url)

        if documents_read:
            time.sleep(POLITE_DELAY_SECONDS)

        result = fetch.fetch_page(
            document_url,
            user_agent=agent,
            timeout=timeout,
            max_bytes=MAX_SITEMAP_BYTES,
            accept="application/xml,text/xml;q=0.9,*/*;q=0.8",
        )
        documents_read += 1

        if result["outcome"] != "retrieved":
            observations.append(
                f"Sitemap candidate {document_url}: outcome={result['outcome']} "
                f"status={result['http_status']}."
            )
            continue

        parsed = parse_sitemap_xml(result.get("body") or "")

        if parsed.get("error"):
            observations.append(f"{document_url}: {parsed['error']}")
            continue

        if parsed["kind"] == "sitemapindex":
            observations.append(
                f"{document_url}: sitemap index listing {len(parsed['entries'])} document(s)."
            )
            for entry in parsed["entries"]:
                if same_origin(entry, origin) and entry not in seen_documents:
                    queue.append(entry)
            continue

        located = [e for e in parsed["entries"] if same_origin(e, origin)]
        observations.append(f"{document_url}: {len(located)} same-origin URL(s).")
        collected.extend(located)

        if len(collected) >= MAX_URLS_SCANNED:
            truncated = True
            collected = collected[:MAX_URLS_SCANNED]
            break

    if queue and documents_read >= MAX_SITEMAP_DOCUMENTS:
        truncated = True
        observations.append(
            f"Stopped after {MAX_SITEMAP_DOCUMENTS} sitemap documents; "
            f"{len(queue)} candidate(s) not read."
        )

    if not collected:
        observations.append(
            "No sitemap URLs were collected. Absence of a sitemap is not a defect "
            "by itself; reachability through crawlable links is assessed separately."
        )

    sample = build_sample(homepage, collected, limit)

    return {
        "origin": origin,
        "sitemaps": sorted(seen_documents),
        "sitemap_documents_read": documents_read,
        "url_count": len(collected),
        "truncated": truncated,
        "sample": sample,
        "sample_strategy": (
            f"Homepage, then up to {limit - 1} sitemap URLs selected by path-role "
            "rank (product, pricing, category, about, contact, service, content), "
            "spread across roles, ties broken lexicographically."
        ),
        "observations": observations,
        "limitations": limitations,
    }


def build_sample(homepage: str, urls: list[str], limit: int) -> list[str]:
    """
    Choose the shared sample deterministically.

    Round-robin across path roles rather than taking the first N of the highest
    rank, so a store with 4000 product URLs does not produce a sample containing
    only product pages and no evidence about anything else.

    Same input, same output. No randomness, no set iteration, no time.
    """
    if limit < 1:
        limit = 1

    unique = sorted(set(u for u in urls if u and u != homepage))

    buckets: dict[int, list[str]] = {}
    for candidate in unique:
        rank, _ = classify(candidate)
        buckets.setdefault(rank, []).append(candidate)

    for rank in buckets:
        buckets[rank].sort()

    sample = [homepage]
    ranks = sorted(buckets)

    while len(sample) < limit and any(buckets[r] for r in ranks):
        for rank in ranks:
            if not buckets[rank]:
                continue
            sample.append(buckets[rank].pop(0))
            if len(sample) >= limit:
                break

    return sample


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover sitemaps and emit the shared deterministic page sample."
    )
    parser.add_argument("url")
    parser.add_argument("--limit", type=int, default=DEFAULT_SAMPLE_LIMIT)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--user-agent", default=None)
    parser.add_argument(
        "--emit-sample",
        action="store_true",
        help="Print only the sample contract object consumed by specialists.",
    )

    args = parser.parse_args()

    robots, robots_error = load_sibling("robots")
    declared: list[str] = []
    robots_limitation = None

    if robots is None:
        robots_limitation = robots_error
    else:
        policy = robots.check_robots(url=args.url, timeout=args.timeout)
        declared = policy.get("sitemaps", [])
        if policy.get("crawl_permission") == "disallowed":
            robots_limitation = (
                "Target path is disallowed by robots.txt; sitemap discovery was "
                "not attempted."
            )
            declared = []

    if robots_limitation and robots is None:
        result = discover(args.url, [], limit=args.limit, timeout=args.timeout,
                          user_agent=args.user_agent)
        result["limitations"].append({
            "check": "robots_policy",
            "reason": robots_limitation,
            "affected_checks": ["sitemap_discovery"],
        })
    elif robots_limitation:
        origin = origin_of(args.url)
        result = {
            "origin": origin,
            "sitemaps": [],
            "sitemap_documents_read": 0,
            "url_count": 0,
            "truncated": False,
            "sample": [origin + "/"],
            "sample_strategy": "Homepage only: crawling is disallowed by robots.txt.",
            "observations": [],
            "limitations": [{
                "check": "sitemap_discovery",
                "reason": robots_limitation,
                "affected_checks": ["page_sample"],
            }],
        }
    else:
        result = discover(args.url, declared, limit=args.limit,
                          timeout=args.timeout, user_agent=args.user_agent)

    if args.emit_sample:
        print(json.dumps({
            "origin": result["origin"],
            "strategy": result["sample_strategy"],
            "pages_inspected": len(result["sample"]),
            "urls": result["sample"],
            "url_count_available": result["url_count"],
            "truncated": result["truncated"],
            "limitations": result["limitations"],
        }, indent=2))
        return 0

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())