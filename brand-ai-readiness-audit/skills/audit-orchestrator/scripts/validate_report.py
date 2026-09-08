#!/usr/bin/env python3
"""
validate_report.py -- contract enforcement for the final audit report.

Two layers of checking:

    1. Schema conformance against references/schema.json. Uses the `jsonschema`
       package when it is installed; otherwise falls back to a built-in
       validator covering the subset of JSON Schema the report actually uses.
       The marketplace must be self-contained, so a missing dependency degrades
       rather than fails.

    2. Invariants JSON Schema cannot express:
         - finding IDs are unique, contiguous and start at F001;
         - summary counts equal the actual severity distribution;
         - findings are in canonical order;
         - every severity matches the score its own severity_inputs produce;
         - every blocked_by and suppressed_findings reference resolves;
         - no finding carries low confidence.

    Layer 2 is the one that catches real regressions. A report can be schema
    valid and still be internally inconsistent, and an inconsistent report is
    worse than a missing one because it looks authoritative.

Exit codes: 0 valid, 1 invalid, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def default_schema_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "references", "schema.json"))


# ---------------------------------------------------------------------------
# Layer 1: schema conformance
# ---------------------------------------------------------------------------

def validate_schema(instance, schema, path: str = "$") -> list[str]:
    """
    Minimal JSON Schema validator: type, required, enum, const, pattern,
    minimum/maximum, minItems/minLength, properties, items.

    Deliberately not a complete implementation. It covers exactly what
    references/schema.json uses, and any construct it does not understand is
    skipped rather than guessed at.
    """
    errors: list[str] = []

    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        checks = {
            "object": dict, "array": list, "string": str,
            "integer": int, "number": (int, float), "boolean": bool,
        }
        ok = False
        for candidate in types:
            if candidate == "null":
                ok = ok or instance is None
            elif candidate == "integer":
                ok = ok or (isinstance(instance, int) and not isinstance(instance, bool))
            elif candidate in checks:
                ok = ok or isinstance(instance, checks[candidate])
        if not ok:
            errors.append(f"{path}: expected type {expected}, got {type(instance).__name__}")
            return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")

    if isinstance(instance, str):
        if "pattern" in schema:
            import re
            if not re.search(schema["pattern"], instance):
                errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} above maximum {schema['maximum']}")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property '{key}'")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in instance:
                errors += validate_schema(instance[key], subschema, f"{path}.{key}")
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            declared = set((schema.get("properties") or {}).keys())
            for key, value in instance.items():
                if key not in declared:
                    errors += validate_schema(value, extra, f"{path}.{key}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        subschema = schema.get("items")
        if isinstance(subschema, dict):
            for index, item in enumerate(instance):
                errors += validate_schema(item, subschema, f"{path}[{index}]")

    return errors


def run_schema_layer(report: dict, schema: dict) -> tuple[list[str], str]:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return validate_schema(report, schema), "built-in"

    validator = jsonschema.Draft202012Validator(schema)
    errors = [
        f"${''.join(f'.{p}' if isinstance(p, str) else f'[{p}]' for p in e.path)}: {e.message}"
        for e in sorted(validator.iter_errors(report), key=lambda e: list(e.path))
    ]
    return errors, "jsonschema"


# ---------------------------------------------------------------------------
# Layer 2: internal invariants
# ---------------------------------------------------------------------------

def sort_key(finding: dict) -> tuple:
    urls = sorted(finding.get("affected_urls") or [""])
    return (
        -SEVERITY_RANK.get(finding.get("severity", ""), 0),
        -(finding.get("severity_inputs") or {}).get("stage_block", 0),
        finding.get("category", ""),
        urls[0] if urls else "",
        finding.get("title", ""),
    )


def band(score: int) -> str:
    if score >= 27:
        return "critical"
    if score >= 14:
        return "high"
    if score >= 6:
        return "medium"
    return "low"


def validate_invariants(report: dict) -> list[str]:
    errors: list[str] = []
    findings = report.get("findings") or []

    ids = [f.get("id") for f in findings]
    if len(ids) != len(set(ids)):
        errors.append("findings: duplicate finding IDs")

    expected_ids = [f"F{i:03d}" for i in range(1, len(findings) + 1)]
    if ids != expected_ids:
        errors.append(
            f"findings: IDs are not contiguous from F001 (got {ids[:5]}...)"
        )

    ordered = sorted(findings, key=sort_key)
    if [f.get("title") for f in ordered] != [f.get("title") for f in findings]:
        errors.append(
            "findings: not in canonical order "
            "(severity desc, stage_block desc, category, url, title)"
        )

    summary = report.get("summary") or {}
    counts = {level: 0 for level in ("critical", "high", "medium", "low")}
    for finding in findings:
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1

    for level, count in counts.items():
        if summary.get(level) != count:
            errors.append(
                f"summary.{level}: reports {summary.get(level)}, findings contain {count}"
            )

    if summary.get("total_findings") != len(findings):
        errors.append(
            f"summary.total_findings: reports {summary.get('total_findings')}, "
            f"findings contain {len(findings)}"
        )

    limitations = report.get("limitations") or []
    if "limitations_count" in summary and summary["limitations_count"] != len(limitations):
        errors.append(
            f"summary.limitations_count: reports {summary['limitations_count']}, "
            f"limitations contain {len(limitations)}"
        )

    for finding in findings:
        inputs = finding.get("severity_inputs") or {}
        if not inputs:
            continue
        product = (
            inputs.get("stage_block", 0)
            * inputs.get("fact_criticality", 0)
            * inputs.get("blast_radius", 0)
        )
        if "score" in inputs and inputs["score"] != product:
            errors.append(
                f"{finding.get('id')}: severity_inputs.score {inputs['score']} "
                f"does not equal the product {product}"
            )
        computed = band(product)
        if finding.get("confidence") == "medium" and computed == "critical":
            computed = "high"
        if finding.get("severity") != computed:
            errors.append(
                f"{finding.get('id')}: severity '{finding.get('severity')}' does not "
                f"match the severity model result '{computed}' for score {product}"
            )
        if finding.get("confidence") == "low":
            errors.append(
                f"{finding.get('id')}: low confidence is not permitted on a finding; "
                "record it as a limitation"
            )
        if not (finding.get("evidence") or []):
            errors.append(f"{finding.get('id')}: has no evidence")

    known = set(ids)
    for finding in findings:
        blocked_by = finding.get("blocked_by")
        if blocked_by and blocked_by not in known:
            errors.append(
                f"{finding.get('id')}: blocked_by '{blocked_by}' does not resolve"
            )

    for entry in report.get("suppressed_findings") or []:
        if entry.get("blocked_by") not in known:
            errors.append(
                f"suppressed_findings: blocked_by '{entry.get('blocked_by')}' "
                "does not resolve to a reported finding"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an audit report against the schema and its invariants."
    )
    parser.add_argument("report", nargs="?", default="-")
    parser.add_argument("--schema", default=None)
    parser.add_argument(
        "--quiet", action="store_true",
        help="Print nothing on success; useful in a pipeline.",
    )

    args = parser.parse_args()

    raw = sys.stdin.read() if args.report == "-" else open(
        args.report, "r", encoding="utf-8"
    ).read()

    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"validate_report.py: input is not valid JSON: {exc}", file=sys.stderr)
        return 2

    schema_path = args.schema or default_schema_path()
    try:
        with open(schema_path, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
    except Exception as exc:
        print(f"validate_report.py: cannot read schema at {schema_path}: {exc}",
              file=sys.stderr)
        return 2

    schema_errors, engine = run_schema_layer(report, schema)
    invariant_errors = validate_invariants(report)
    errors = schema_errors + invariant_errors

    if errors:
        print(
            json.dumps({
                "valid": False,
                "engine": engine,
                "schema_errors": schema_errors,
                "invariant_errors": invariant_errors,
            }, indent=2),
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(json.dumps({
            "valid": True,
            "engine": engine,
            "findings": len(report.get("findings") or []),
            "limitations": len(report.get("limitations") or []),
        }, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())