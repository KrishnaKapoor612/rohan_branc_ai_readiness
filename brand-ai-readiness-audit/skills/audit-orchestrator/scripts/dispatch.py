#!/usr/bin/env python3
"""
dispatch.py -- marketplace-level specialist runner for audit-orchestrator.

Reads marketplace.json, collects shared evidence ONCE via evidence.py, runs
every non-entrypoint skill through the specialist contract against that same
evidence, and emits one aggregate object for merge.py.

Knows nothing about what any specialist checks. Adding a skill to the manifest
is sufficient to have it run; no code here changes.

This module does NOT:
    - perform website checks itself;
    - assign severities or finding IDs;
    - abort the audit because one specialist failed;
    - let a specialist fetch its own copy of a sampled page.

See references/specialist-contract.md for the interface enforced here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from typing import Optional

DEFAULT_SPECIALIST_TIMEOUT = 120
DEFAULT_EVIDENCE_TIMEOUT = 90
DEFAULT_SAMPLE_LIMIT = 12
CHECK_SCRIPT = os.path.join("scripts", "check.py")
EVIDENCE_SCRIPT = os.path.join("skills", "audit-orchestrator", "scripts", "evidence.py")

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


def build_evidence(
    root: str, url: str, work_dir: str, limit: int, timeout: int, no_render: bool,
) -> tuple[Optional[str], dict, Optional[dict]]:
    """
    Run evidence.py once. Returns (evidence_file_or_None, summary, limitation_or_None).

    A failure here does not abort the audit: specialists still run and record
    their own limitations against a missing/empty evidence file, exactly as
    they would against any other collection gap.
    """
    script = os.path.join(root, EVIDENCE_SCRIPT)

    if not os.path.isfile(script):
        return None, {}, {
            "skill": "dispatch",
            "check": "evidence_collection",
            "reason": f"evidence.py not found at {script}.",
            "affected_checks": ["all"],
        }

    command = [
        sys.executable, script, url,
        "--limit", str(limit),
        "--timeout", str(min(timeout, 30)),
        "--work-dir", work_dir,
    ]
    if no_render:
        command.append("--no-render")

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return None, {}, {
            "skill": "dispatch",
            "check": "evidence_collection",
            "reason": f"Evidence collection exceeded {timeout}s and was terminated. "
                      "Specialists ran without shared evidence.",
            "affected_checks": ["all"],
        }
    except Exception as exc:
        return None, {}, {
            "skill": "dispatch",
            "check": "evidence_collection",
            "reason": f"Evidence collection failed: {type(exc).__name__}: {exc}",
            "affected_checks": ["all"],
        }

    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, {}, {
            "skill": "dispatch",
            "check": "evidence_collection",
            "reason": "evidence.py did not produce parseable stdout. "
                      f"stderr: {(completed.stderr or '').strip()[:400]}",
            "affected_checks": ["all"],
        }

    evidence_file = summary.get("evidence_file")
    if not evidence_file or not os.path.isfile(evidence_file):
        return None, summary, {
            "skill": "dispatch",
            "check": "evidence_collection",
            "reason": "evidence.py reported no usable evidence_file.",
            "affected_checks": ["all"],
        }

    return evidence_file, summary, None


def run_specialist(root: str, skill: dict, url: str, evidence_file: Optional[str],
                   timeout: int, no_render: bool) -> dict:
    """
    Execute one specialist against the shared evidence file. Any failure
    becomes a well-formed failure result, never an exception that ends the
    audit.
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
    if evidence_file:
        command += ["--evidence-file", evidence_file]
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
        description="Run every specialist skill declared in marketplace.json "
                    "against one shared evidence collection."
    )
    parser.add_argument("url")
    parser.add_argument("--marketplace-root", default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_SPECIALIST_TIMEOUT)
    parser.add_argument("--evidence-timeout", type=int, default=DEFAULT_EVIDENCE_TIMEOUT)
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

    # Every audit gets its own working directory so concurrent audits never
    # collide and so evidence.json is never committed to the repository.
    work_dir = args.work_dir or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"brand-ai-audit-{uuid.uuid4().hex[:12]}"
    )
    os.makedirs(work_dir, exist_ok=True)

    evidence_file, evidence_summary, evidence_limitation = build_evidence(
        root, args.url, work_dir, args.sample_limit, args.evidence_timeout, args.no_render,
    )
    if evidence_limitation:
        limitations.append(evidence_limitation)

    # `sample` is kept in the aggregate/report for backward compatibility with
    # merge.py and schema.json, which read audit.sample. It is now sourced
    # from evidence.py's own collection rather than a separate sampler run.
    sample = {
        "strategy": "See site_level.sitemap.strategy in the shared evidence file.",
        "pages_inspected": evidence_summary.get("pages_sampled", 0),
        "urls": [],
    }
    if evidence_file:
        try:
            with open(evidence_file, "r", encoding="utf-8") as handle:
                evidence_doc = json.load(handle)
            sample["urls"] = [p.get("url") for p in evidence_doc.get("pages", [])]
            sample["strategy"] = evidence_doc.get("site_level", {}).get(
                "sitemap", {}
            ).get("strategy", sample["strategy"])
        except Exception:
            pass

    results = []
    for skill in specialists:
        results.append(
            run_specialist(root, skill, args.url, evidence_file,
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
        "evidence_file": evidence_file,
        "sample": sample,
        "results": results,
        "limitations": limitations,
        "proactive_recommendations": proactive,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
