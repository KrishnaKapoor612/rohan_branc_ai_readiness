---
name: audit-orchestrator
description: Entrypoint skill for the brand AI readiness audit marketplace. Given a public website URL, collects shared website evidence, runs the marketplace specialist skills against that evidence, combines their results, validates the final report, and emits one JSON audit report covering AI discoverability and on-site engagement.
license: Apache-2.0
allowed-tools: Bash
compatibility: Requires a skills-compatible agent runtime able to execute the bundled python 3.9+ scripts.
---

# Audit Orchestrator

## When to use

Use this skill when a public website needs to be audited for:

- AI discoverability
- on-site engagement

This is the single entrypoint for the `brand-ai-readiness-audit` marketplace.

## Inputs

Required:

- A public website URL or domain.

Optional runtime options supported by the entrypoint:

- `--sample-limit` — maximum number of pages included in the shared evidence sample.
- `--timeout` — timeout used for specialist execution.
- `--evidence-timeout` — timeout used for shared evidence collection.
- `--work-dir` — runtime working directory.
- `--no-render` — disable optional rendering evidence.
- `--marketplace-root` — explicit marketplace root.

The entrypoint is responsible for passing these options to the appropriate
parts of the audit workflow.

## Procedure

1. **Validate the audit request.**
   - Require a non-empty website URL or domain.
   - Reject an invalid target before running specialist checks.

2. **Locate and validate the marketplace.**
   - Locate `marketplace.json`.
   - Read the marketplace manifest.
   - Require exactly one skill marked as the entrypoint.
   - Treat the other manifest-listed skills as specialist skills.
   - Do not execute the entrypoint recursively.

3. **Run the marketplace dispatch workflow.**
   - Execute `scripts/dispatch.py` with the target URL and supported runtime
     options.
   - `dispatch.py` coordinates the audit and does not perform specialist
     checks itself.

4. **Collect shared website evidence.**
   - `dispatch.py` invokes `scripts/evidence.py` once for the audit.
   - `evidence.py` coordinates the shared collection modules:
     - `robots.py`
     - `sitemap.py`
     - `fetch.py`
     - `render.py`
   - Respect `robots.txt` before retrieving sampled pages.
   - Keep collection bounded by the configured limits.
   - Write the collected evidence to a runtime `evidence.json`.

5. **Run the specialist skills.**
   - Run every non-entrypoint specialist declared in `marketplace.json`.
   - Pass the shared evidence file to each specialist through the specialist
     contract.
   - Specialists analyze the shared evidence rather than independently
     fetching the sampled website pages.
   - A specialist must not treat missing or incomplete evidence as proof of a
     website defect.

6. **Handle specialist failures.**
   - Isolate failures, timeouts and contract violations.
   - Record incomplete checks as limitations.
   - Do not convert a failed or unverified check into a confirmed finding.

7. **Merge specialist results.**
   - Pass the aggregate specialist results to `scripts/merge.py`.
   - Deduplicate findings where they represent the same underlying defect.
   - Preserve supporting evidence and limitations.
   - Apply the marketplace's implemented finding composition and severity
     rules.
   - Assign final finding IDs after merging.

8. **Create the final report.**
   - Supply the audit timestamp required by `merge.py`.
   - Produce the canonical report defined by `references/schema.json`.

9. **Validate the final report.**
   - Run `scripts/validate_report.py` against the merged report.
   - Do not invent evidence or findings to satisfy validation.
   - A report that cannot be validated must not be presented as a valid final
     report.

10. **Emit the final audit report.**
    - Emit one validated JSON report.
    - The report must conform to `references/schema.json`.
    - The report includes the Round-3 required fields:
      - `site`
      - `audited_at`
      - `summary`
      - `findings`
    - Each finding includes:
      - `id`
      - `title`
      - `severity`
      - `evidence`
      - `suggested_action`

## Detection quality

Findings must be supported by evidence actually collected during the audit.

- Do not treat missing evidence as proof of a defect.
- Do not treat the absence of an optional implementation detail as a defect
  unless the evidence demonstrates a material problem.
- Keep the affected scope consistent with the pages actually inspected.
- Prefer a limitation when a check could not be completed.
- Prefer fewer well-supported findings over speculative findings.
- Do not claim third-party corroboration when it was not actually assessed.

Examples of signals that must not automatically become defects:

- absence of JSON-LD;
- absence of `sameAs`;
- absence of a date signal;
- absence of an on-site search function;
- absence of heading IDs.

The evidence and specialist logic must establish the actual problem before a
finding is emitted.

## Shared evidence

The collection and analysis layers are separate:

```text
dispatch.py
    |
    v
evidence.py
    |
    +-- robots.py
    +-- sitemap.py
    +-- fetch.py
    +-- render.py
    |
    v
evidence.json
    |
    +-- specialist check.py
    +-- specialist check.py
    +-- specialist check.py
    |
    v
merge.py
    |
    v
validate_report.py
    |
    v
final audit report
```


## Bundled resources

| Path | Purpose |
|---|---|
| `scripts/dispatch.py` | Manifest-driven specialist execution, shared evidence coordination, and fault isolation |
| `scripts/evidence.py` | Collects and normalizes shared website evidence once for all specialists |
| `scripts/fetch.py` | Low-level read-only HTTP retrieval used by the shared evidence collector |
| `scripts/robots.py` | Reads and evaluates `robots.txt` before permitted retrieval |
| `scripts/sitemap.py` | Discovers and bounds candidate URLs for shared evidence collection |
| `scripts/render.py` | Optional bounded rendered-page evidence collection |
| `scripts/merge.py` | Deduplication, funnel gating, severity scoring, and canonical ordering |
| `scripts/validate_report.py` | Schema conformance plus internal invariants; no external dependency required |
| `references/schema.json` | The canonical report schema |
| `references/severity-model.md` | The severity arithmetic, its tables, and its refusal rules |
| `references/specialist-contract.md` | The interface every specialist implements |

## Safety and operational guardrails

- Recommend-only. No skill in this marketplace alters a live site.
- Read-only, GET-only collection; no authentication and no bypassing `robots.txt`, CAPTCHA, WAF, or paywalls.
- Bounded requests, bytes, redirects, rendering, and execution time; polite delays; `robots.txt` respected before permitted retrieval.
- Shared website evidence is collected once and reused by all specialist skills.
- Specialists must not independently fetch the same sampled pages already covered by shared evidence.
- Collection or specialist failures are recorded as limitations rather than converted into findings.
- Findings must be supported by evidence actually collected during the audit.