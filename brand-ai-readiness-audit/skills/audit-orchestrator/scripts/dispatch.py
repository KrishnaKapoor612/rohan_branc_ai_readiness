#!/usr/bin/env python3
"""
dispatch.py -- marketplace-level specialist runner for audit-orchestrator.

Reads marketplace.json, builds the shared page sample, runs every non-entrypoint
skill through the specialist contract, and emits one aggregate object for
merge.py.

Knows nothing about what any specialist checks. Adding a skill to the manifest
is sufficient to have it run; no code here changes.

This module does NOT:
    - perform website checks itself;
    - assign severities or finding IDs;
    - abort the audit because one specialist failed.

See references/specialist-contract.md for the interface enforced here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

DEFAULT_SPECIALIST_TIMEOUT = 120
DEFAULT_SAMPLE_LIMIT = 12
CHECK_SCRIPT = os.path.join("scripts", "check.py")
SAMPLER_SCRIPT = os.path.join("scripts", "sitemap.py")


def marketplace_root_default() -> str:
    """scripts/ -> audit-orchestrator/ -> skills/ -> marketplace root."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def read_manifest(root: str) -> dict:
    path = os.path.join(root, "marketplace.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_manifest(manifest: dict) -> tuple[dict, list[dict]]:
    """
    Enforce the two structural rules the contest states: every skill is listed,
    and exactly one is the entrypoint.
    """
    skills = manifest.get("skills") or []
    if not skills:
        raise ValueError("marketplace.json lists no skills.")

    entrypoints = [s for s in skills if s.get("entrypoint") is True]
    if len(entrypoints) != 1:
        raise ValueError(
            f"marketplace.json must mark exactly one entrypoint; found {len(entrypoints)}."
        )

    specialists = [s for s in skills if s.get("entrypoint") is not True]
    return entrypoints[0], specialists


def find_sampler(specialists: list[dict]) -> Optional[dict]:
    """
    The sample producer is declared in the manifest, not hardcoded:

        { "id": "crawl-render-audit", "path": "...", "provides": ["page_sample"] }

    Falls back to the first specialist that ships a scripts/sitemap.py, so a
    manifest written before `provides` existed still produces a shared sample.
    """
    for skill in specialists:
        if "page_sample" in (skill.get("provides") or []):
            return skill
    return None


def find_sampler_fallback(root: str, specialists: list[dict]) -> Optional[dict]:
    """Manifest-free fallback: the first specialist that ships a sampler script."""
    for skill in specialists:
        candidate = os.path.join(root, skill.get("path", ""), SAMPLER_SCRIPT)
        if os.path.isfile(candidate):
            return skill
    return None


def build_sample(root: str, sampler: Optional[dict], url: str,
                 limit: int, timeout: int) -> tuple[dict, Optional[dict]]:
    """Run the sampler and return (sample, limitation_or_None)."""
    fallback = {
        "origin": url,
        "strategy": "Target URL only: no page sampler available.",
        "pages_inspected": 1,
        "urls": [url],
        "url_count_available": 0,
        "truncated": False,
        "limitations": [],
    }

    if sampler is None:
        return fallback, {
            "skill": "dispatch",
            "check": "page_sample",
            "reason": "No skill in the manifest declares provides: page_sample.",
            "affected_checks": ["scope_claims"],
        }

    script = os.path.join(root, sampler["path"], SAMPLER_SCRIPT)
    if not os.path.isfile(script):
        return fallback, {
            "skill": sampler["id"],
            "check": "page_sample",
            "reason": f"Sampler script not found at {script}.",
            "affected_checks": ["scope_claims"],
        }

    try:
        completed = subprocess.run(
            [sys.executable, script, url, "--limit", str(limit), "--emit-sample"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        sample = json.loads(completed.stdout)
        return sample, None
    except subprocess.TimeoutExpired:
        return fallback, {
            "skill": sampler["id"],
            "check": "page_sample",
            "reason": f"Sampler exceeded {timeout}s; the audit fell back to the target URL only.",
            "affected_checks": ["scope_claims"],
        }
    except Exception as exc:
        return fallback, {
            "skill": sampler["id"],
            "check": "page_sample",
            "reason": f"Sampler failed: {type(exc).__name__}: {exc}",
            "affected_checks": ["scope_claims"],
        }


def run_specialist(root: str, skill: dict, url: str, sample_file: Optional[str],
                   timeout: int, no_render: bool) -> dict:
    """
    Execute one specialist. Any failure becomes a well-formed failure result,
    never an exception that ends the audit.
    """
    skill_id = skill.get("id", "unknown")
    script = os.path.join(root, skill.get("path", ""), CHECK_SCRIPT)

    def failure(reason: str, check: str = "specialist_execution") -> dict:
        return {
            "skill": skill_id,
            "status": "failure",
            "observations": [],
            "findings": [],
            "limitations": [{
                "check": check,
                "reason": reason,
                "affected_checks": ["all"],
            }],
            "duration_ms": 0,
        }

    if not os.path.isfile(script):
        return failure(f"No check.py found at {script}.", "specialist_discovery")

    command = [sys.executable, script, url]
    if sample_file:
        command += ["--sample-file", sample_file]
    if no_render:
        command.append("--no-render")

    started = time.monotonic()

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return failure(
            f"Specialist exceeded its {timeout}s timeout and was terminated.",
            "time_budget",
        )
    except Exception as exc:
        return failure(f"Could not execute specialist: {type(exc).__name__}: {exc}")

    elapsed = int((time.monotonic() - started) * 1000)

    if not completed.stdout.strip():
        return failure(
            "Specialist produced no output on stdout. "
            f"stderr: {(completed.stderr or '').strip()[:400]}",
            "contract_violation",
        )

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return failure(
            f"Specialist stdout was not valid JSON ({exc}). "
            f"First 200 characters: {completed.stdout[:200]!r}",
            "contract_violation",
        )

    for required in ("skill", "status", "findings", "limitations"):
        if required not in result:
            return failure(
                f"Specialist result is missing the required field '{required}'.",
                "contract_violation",
            )

    result.setdefault("observations", [])
    result.setdefault("proactive_recommendations", [])
    result["duration_ms"] = elapsed

    if completed.stderr.strip():
        result["observations"].append(
            f"stderr: {completed.stderr.strip()[:400]}"
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run every specialist skill declared in marketplace.json."
    )
    parser.add_argument("url")
    parser.add_argument("--marketplace-root", default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_SPECIALIST_TIMEOUT)
    parser.add_argument("--sample-limit", type=int, default=DEFAULT_SAMPLE_LIMIT)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--no-render", action="store_true")

    args = parser.parse_args()

    root = os.path.abspath(args.marketplace_root or marketplace_root_default())
    started = time.monotonic()

    try:
        manifest = read_manifest(root)
        _, specialists = validate_manifest(manifest)
    except Exception as exc:
        print(json.dumps({
            "url": args.url,
            "marketplace_root": root,
            "error": f"{type(exc).__name__}: {exc}",
            "results": [],
            "limitations": [{
                "skill": "dispatch",
                "check": "manifest",
                "reason": f"{type(exc).__name__}: {exc}",
                "affected_checks": ["all"],
            }],
        }, indent=2))
        return 2

    limitations: list[dict] = []

    sampler = find_sampler(specialists) or find_sampler_fallback(root, specialists)
    sample, sample_limitation = build_sample(
        root, sampler, args.url, args.sample_limit, args.timeout
    )
    if sample_limitation:
        limitations.append(sample_limitation)
    for entry in sample.get("limitations", []):
        limitations.append({
            "skill": (sampler or {}).get("id", "dispatch"),
            "check": entry.get("check", "page_sample"),
            "reason": entry.get("reason", ""),
            "affected_checks": entry.get("affected_checks", []),
        })

    work_dir = args.work_dir or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "brand-ai-readiness-audit"
    )
    os.makedirs(work_dir, exist_ok=True)
    sample_file = os.path.join(work_dir, "sample.json")

    with open(sample_file, "w", encoding="utf-8") as handle:
        json.dump(sample, handle, indent=2)

    results = []
    for skill in specialists:
        results.append(
            run_specialist(root, skill, args.url, sample_file,
                           args.timeout, args.no_render)
        )

    for result in results:
        for entry in result.get("limitations", []):
            limitations.append({
                "skill": result.get("skill", "unknown"),
                "check": entry.get("check", "unspecified"),
                "reason": entry.get("reason", ""),
                "affected_checks": entry.get("affected_checks", []),
            })

    proactive: list[dict] = []
    for result in results:
        for item in result.get("proactive_recommendations", []):
            entry = dict(item)
            entry.setdefault("source_skill", result.get("skill", "unknown"))
            proactive.append(entry)

    print(json.dumps({
        "url": args.url,
        "marketplace": manifest.get("name"),
        "marketplace_version": manifest.get("version"),
        "marketplace_root": root,
        "sample": sample,
        "sample_file": sample_file,
        "results": results,
        "limitations": limitations,
        "proactive_recommendations": proactive,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())