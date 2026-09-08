#!/usr/bin/env python3
"""
merge.py -- deduplicate, gate, score and number findings into the audit report.

This is where the marketplace's reasoning lives. Specialists observe; this
module decides what the audit actually claims.

Four things happen here that no specialist can do alone:

    1. Deduplication      - the same root cause seen by two skills becomes one
                            finding carrying both sets of evidence.
    2. Funnel gating      - a downstream check that failed only because an
                            upstream stage blocks it is not reported as an
                            independent defect. It moves to suppressed_findings
                            with a reference to its blocker.
    3. Severity scoring   - severity_inputs are multiplied and banded per
                            references/severity-model.md. No judgement, no model
                            call, no freehand labels.
    4. Canonical ordering - findings are sorted by a fully specified key, then
                            numbered F001, F002, ... so IDs are stable.

This module does NOT:
    - invent findings, evidence or actions;
    - discard limitations;
    - use randomness, timestamps or hash order anywhere in an identifier.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Optional
from urllib.parse import urlsplit

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
PRIORITY_ORDER = ["low", "medium", "high"]

# Two kinds of blocker, with different reach.
#
# A HARD access failure (403, 5xx, TLS error, timeout, challenge page) means no
# check could physically run against the page. It gates everything downstream,
# including engagement.
#
# A POLICY block (a robots.txt disallow) means a compliant machine reader is
# refused, but the page itself is still retrievable and is still served to human
# visitors. It gates the discoverability chain only. Engagement defects remain
# directly observable and must still be reported, because a visitor arriving
# from search, social or a direct link still meets them.
BLOCKING_STAGES = {"reachable"}
HARD_BLOCK_CATEGORIES = {"access"}
POLICY_BLOCK_CATEGORIES = {"crawl_policy"}

# Stages that depend on a machine reader being permitted to fetch the page.
DISCOVERY_STAGES = {
    "renderable", "extractable", "unambiguous",
    "corroborated", "current", "actionable",
}



# ---------------------------------------------------------------------------
# Severity model (references/severity-model.md)
# ---------------------------------------------------------------------------

def band(score: int) -> str:
    if score >= 27:
        return "critical"
    if score >= 14:
        return "high"
    if score >= 6:
        return "medium"
    return "low"


def cap_for_confidence(severity: str, confidence: str) -> str:
    """medium confidence cannot yield critical. low confidence is not a finding."""
    if confidence == "medium" and severity == "critical":
        return "high"
    return severity


def score_finding(finding: dict) -> tuple[int, str]:
    inputs = finding.get("severity_inputs") or {}
    stage_block = int(inputs.get("stage_block", 1))
    criticality = int(inputs.get("fact_criticality", 1))
    radius = int(inputs.get("blast_radius", 1))

    stage_block = max(1, min(4, stage_block))
    criticality = max(1, min(3, criticality))
    radius = max(1, min(3, radius))

    score = stage_block * criticality * radius
    severity = cap_for_confidence(band(score), finding.get("confidence", "high"))
    return score, severity


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").lower()).strip()


def dedup_key(finding: dict) -> str:
    """
    Two findings are the same defect when they name the same category, the same
    funnel stage, and the same affected surface. Titles are normalised so that
    wording differences between skills do not split one root cause in two.
    """
    urls = ",".join(sorted(finding.get("affected_urls") or []))
    return "|".join([
        finding.get("category", "uncategorised"),
        finding.get("stage", "unknown"),
        normalize_title(finding.get("title", "")),
        urls,
    ])


def merge_pair(primary: dict, other: dict) -> dict:
    """
    Keep the stronger claim. Evidence is unioned in stable order; the higher
    severity_inputs win, because the wider observation is the truer one.
    """
    merged = dict(primary)

    seen = list(primary.get("evidence") or [])
    for item in other.get("evidence") or []:
        if item not in seen:
            seen.append(item)
    merged["evidence"] = seen

    urls = sorted(set(primary.get("affected_urls") or []) |
                  set(other.get("affected_urls") or []))
    merged["affected_urls"] = urls

    a = primary.get("severity_inputs") or {}
    b = other.get("severity_inputs") or {}
    merged["severity_inputs"] = {
        "stage_block": max(a.get("stage_block", 1), b.get("stage_block", 1)),
        "fact_criticality": max(a.get("fact_criticality", 1), b.get("fact_criticality", 1)),
        "blast_radius": max(a.get("blast_radius", 1), b.get("blast_radius", 1)),
    }

    # The weaker confidence governs the merged claim.
    order = {"high": 2, "medium": 1, "low": 0}
    if order.get(other.get("confidence", "high"), 2) < order.get(
        primary.get("confidence", "high"), 2
    ):
        merged["confidence"] = other.get("confidence")

    sources = sorted(set(
        [primary.get("source_skill")] + [other.get("source_skill")]
    ) - {None})
    merged["source_skill"] = ", ".join(sources)

    return merged


# ---------------------------------------------------------------------------
# Funnel gating
# ---------------------------------------------------------------------------

def covers(blocker: dict, candidate: dict) -> bool:
    """
    Does the blocker's scope contain the candidate's affected surface?

    site_wide and template cover everything on the origin. section covers URLs
    sharing the blocker's first path segment. single_page covers only exact URLs.
    """
    scope = blocker.get("scope", "single_page")
    blocker_urls = blocker.get("affected_urls") or []
    candidate_urls = candidate.get("affected_urls") or []

    if scope in ("site_wide", "template"):
        return True

    if not blocker_urls or not candidate_urls:
        return False

    if scope == "section":
        prefixes = {
            "/" + (urlsplit(u).path or "/").strip("/").split("/")[0]
            for u in blocker_urls
        }
        return all(
            any((urlsplit(c).path or "/").startswith(p) for p in prefixes)
            for c in candidate_urls
        )

    return set(candidate_urls).issubset(set(blocker_urls))


def gates(blocker: dict, candidate: dict) -> bool:
    """
    Does this blocker's reach extend to this candidate's stage?

    A hard access failure gates every later stage: nothing could be measured.
    A policy block gates only the discoverability chain, never post-arrival
    engagement, because the page is still served to visitors and its
    orientation defects are still directly observable.
    """
    category = blocker.get("category")

    if category in HARD_BLOCK_CATEGORIES:
        return True

    if category in POLICY_BLOCK_CATEGORIES:
        return candidate.get("stage") in DISCOVERY_STAGES

    # Unknown blocker category: gate conservatively, discoverability only.
    return candidate.get("stage") in DISCOVERY_STAGES


def apply_gating(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split findings into (reported, suppressed).

    A downstream finding is suppressed when an upstream blocker makes it
    impossible to say whether the downstream defect is real independently of
    the blocker. This is the marketplace's main defence against reporting one
    root cause four times and inflating the severity counts.
    """
    blockers = [f for f in findings if f.get("stage") in BLOCKING_STAGES]
    if not blockers:
        return findings, []

    reported: list[dict] = []
    suppressed: list[dict] = []

    for finding in findings:
        if finding.get("stage") in BLOCKING_STAGES:
            reported.append(finding)
            continue

        blocker = next(
            (b for b in blockers
             if covers(b, finding) and gates(b, finding)),
            None,
        )

        if blocker is None:
            reported.append(finding)
            continue

        blocker["_blocks"] = blocker.get("_blocks", 0) + 1
        suppressed.append({
            "title": finding.get("title"),
            "stage": finding.get("stage"),
            "blocked_by_key": dedup_key(blocker),
            "reason": (
                f"Not reported independently: {blocker.get('title')} prevents this "
                "check from being verified on its own. Fix the blocker, then re-run."
            ),
        })

    return reported, suppressed


# ---------------------------------------------------------------------------
# Ordering and numbering
# ---------------------------------------------------------------------------

def sort_key(finding: dict) -> tuple:
    """
    Canonical order, fully specified so IDs never drift between runs:
    severity desc, stage_block desc, category asc, first URL asc, title asc.
    """
    urls = sorted(finding.get("affected_urls") or [""])
    return (
        -SEVERITY_RANK.get(finding["severity"], 0),
        -(finding.get("severity_inputs") or {}).get("stage_block", 0),
        finding.get("category", ""),
        urls[0] if urls else "",
        finding.get("title", ""),
    )


def priority_for(finding: dict) -> str:
    base = "high" if finding["severity"] in ("critical", "high") else (
        "medium" if finding["severity"] == "medium" else "low"
    )
    if finding.get("_blocks"):
        index = min(PRIORITY_ORDER.index(base) + 1, len(PRIORITY_ORDER) - 1)
        return PRIORITY_ORDER[index]
    return base


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def headline(findings: list[dict], limitations: list[dict]) -> str:
    if not findings:
        if limitations:
            return (
                f"No defects confirmed, but {len(limitations)} check(s) could not be "
                "completed, so this result is incomplete rather than clean."
            )
        return "No defects found across the checks that were performed."

    worst = findings[0]
    return (
        f"The earliest blocking problem is at the '{worst.get('stage')}' stage: "
        f"{worst.get('title')}. Fixing it first is what unblocks everything downstream."
    )


def collect_proactive(aggregate: dict) -> list[dict]:
    """
    Deduplicate and order the beyond-problem suggestions specialists offered.

    These are not findings: they carry no severity, no evidence and no funnel
    stage, because they improve discoverability or engagement where no defect
    was detected. Keeping them in their own array is what stops a good idea from
    being mistaken for a measured fault.
    """
    seen: dict[str, dict] = {}

    for item in aggregate.get("proactive_recommendations", []) or []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        key = normalize_title(item["title"])
        if key not in seen:
            seen[key] = item

    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        seen.values(),
        key=lambda i: (rank.get(i.get("priority", "low"), 3),
                       i.get("category", ""),
                       i.get("title", "")),
    )


def build_report(aggregate: dict, audited_at: str, site: Optional[str] = None) -> dict:
    collected: list[dict] = []

    for result in aggregate.get("results", []):
        skill_id = result.get("skill", "unknown")
        for finding in result.get("findings", []):
            item = dict(finding)
            item.setdefault("source_skill", skill_id)
            if item.get("confidence") == "low":
                # Contract rule: low confidence is a limitation, never a finding.
                continue
            collected.append(item)

    # Deduplicate.
    by_key: dict[str, dict] = {}
    for finding in collected:
        key = dedup_key(finding)
        by_key[key] = merge_pair(by_key[key], finding) if key in by_key else finding

    deduped = list(by_key.values())

    # Score before gating: severity decides which blocker wins on ties.
    for finding in deduped:
        score, severity = score_finding(finding)
        finding["severity"] = severity
        finding["severity_inputs"] = dict(finding.get("severity_inputs") or {})
        finding["severity_inputs"]["score"] = score

    reported, suppressed = apply_gating(deduped)

    reported.sort(key=sort_key)

    key_to_id: dict[str, str] = {}
    findings: list[dict] = []

    for index, finding in enumerate(reported, start=1):
        finding_id = f"F{index:03d}"
        key_to_id[dedup_key(finding)] = finding_id

        action = dict(finding.get("suggested_action") or {})
        action["priority"] = priority_for(finding)
        action.setdefault("summary", "No action supplied by the detecting skill.")

        findings.append({
            "id": finding_id,
            "title": finding.get("title"),
            "severity": finding["severity"],
            "evidence": finding.get("evidence") or [],
            "suggested_action": action,
            "stage": finding.get("stage"),
            "category": finding.get("category"),
            "confidence": finding.get("confidence", "high"),
            "affected_urls": sorted(finding.get("affected_urls") or []),
            "scope": finding.get("scope"),
            "severity_inputs": finding["severity_inputs"],
            "blocked_by": None,
            "source_skill": finding.get("source_skill"),
        })

    for entry in suppressed:
        entry["blocked_by"] = key_to_id.get(entry.pop("blocked_by_key"), "unknown")

    limitations = aggregate.get("limitations", [])

    counts = {level: 0 for level in ("critical", "high", "medium", "low")}
    by_stage: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] += 1
        stage = finding.get("stage") or "unknown"
        by_stage[stage] = by_stage.get(stage, 0) + 1

    sample = aggregate.get("sample") or {}

    return {
        "schema_version": "1.1.0",
        "site": site or aggregate.get("url"),
        "audited_at": audited_at,
        "audit": {
            "marketplace": aggregate.get("marketplace"),
            "marketplace_version": aggregate.get("marketplace_version"),
            "skills_run": sorted(
                [
                    {
                        "id": r.get("skill"),
                        "status": r.get("status"),
                        "duration_ms": r.get("duration_ms", 0),
                    }
                    for r in aggregate.get("results", [])
                ],
                key=lambda r: r["id"] or "",
            ),
            "sample": {
                "strategy": sample.get("strategy", "unspecified"),
                "pages_inspected": sample.get("pages_inspected", 0),
                "urls": sample.get("urls", []),
            },
            "duration_ms": aggregate.get("duration_ms", 0),
        },
        "summary": {
            "total_findings": len(findings),
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
            "by_stage": dict(sorted(by_stage.items())),
            "limitations_count": len(limitations),
            "headline": headline(findings, limitations),
        },
        "findings": findings,
        "suppressed_findings": sorted(
            suppressed, key=lambda s: (s.get("blocked_by", ""), s.get("title") or "")
        ),
        "limitations": limitations,
        "proactive_recommendations": collect_proactive(aggregate),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge specialist results into the canonical audit report."
    )
    parser.add_argument(
        "input", nargs="?", default="-",
        help="Aggregate JSON from dispatch.py, or - for stdin.",
    )
    parser.add_argument(
        "--audited-at", required=True,
        help="UTC ISO-8601 timestamp. Supplied by the orchestrator, never generated here.",
    )
    parser.add_argument("--site", default=None)

    args = parser.parse_args()

    raw = sys.stdin.read() if args.input == "-" else open(
        args.input, "r", encoding="utf-8"
    ).read()

    try:
        aggregate = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"merge.py: input was not valid JSON: {exc}", file=sys.stderr)
        return 2

    report = build_report(aggregate, args.audited_at, args.site)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())