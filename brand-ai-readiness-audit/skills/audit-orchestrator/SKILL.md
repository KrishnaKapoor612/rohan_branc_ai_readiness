---
name: audit-orchestrator
description: Entrypoint skill for the brand AI readiness audit marketplace. Given a public website URL, it builds one shared page sample, runs every specialist skill declared in marketplace.json, then deduplicates their findings, gates downstream findings behind upstream blockers, scores severity from a fixed arithmetic model, numbers findings deterministically, validates the result and emits exactly one JSON audit report covering both AI discoverability and on-site engagement. Use this skill whenever a complete website AI-readiness audit is requested, or when asked why a brand is missing, misrepresented or bouncing visitors in AI assistants.
license: Apache-2.0
allowed-tools: Bash
compatibility: Requires a skills-compatible agent runtime able to execute the marketplace's bundled Python 3.9+ scripts. Standard library only; no third-party packages are required and no external service is needed to resolve the marketplace.
---

# Audit Orchestrator

## When to use

This is the single entrypoint for the `brand-ai-readiness-audit` marketplace.
Invoke it with a website URL to produce one audit report.

It composes specialists; it performs no website checks itself.

## The model the marketplace reasons from

Discovery is a funnel, not a switch. A page can pass one stage and fail the
next:

```
exists -> reachable -> renderable -> extractable -> unambiguous
       -> corroborated -> current -> engaging -> actionable
```

Each specialist owns a contiguous band of that funnel, which is why the
decomposition is a separation of concerns rather than padding:

| Skill | Band | Question |
|---|---|---|
| `crawl-render-audit` | reachable, renderable, extractable | can a machine get in, read the page, and pick out a fact? |
| `freshness-corroboration-audit` | unambiguous, corroborated, current | is it clear which entity this is, when it was true, and does anything agree? |
| `engagement-audit` | engaging | once a visitor arrives, can they orient and find what they came for? |

Two consequences follow, and both are the orchestrator's job:

1. **Findings must be attributed to the earliest failing stage.** A price that
   is missing because `/products/` is disallowed is a reachability defect, not
   an extraction defect.
2. **Downstream findings behind an upstream blocker are not independent
   defects.** Reporting them separately turns one root cause into four findings
   and inflates the severity counts.

## Inputs

Required:

- `url` — public website URL or bare domain.

Optional, passed through to `dispatch.py`:

- `--sample-limit` (default 12), `--timeout`, `--work-dir`, `--no-render`,
  `--marketplace-root`.

## Workflow

1. **Validate the request.** Require a non-empty URL or domain. Normalise a bare
   domain to HTTPS. Reject a malformed target before any specialist runs.

2. **Locate the marketplace root** — the directory containing
   `marketplace.json`. Use the runtime's explicit root when one is supplied
   rather than assuming a fixed layout.

3. **Read `marketplace.json`.** Identify every skill and the single one marked
   `"entrypoint": true`. Exactly one entrypoint must exist. Never execute the
   entrypoint recursively. Treat every other listed skill as a specialist.
   Discover specialists from the manifest; never hardcode a list.

4. **Dispatch.** Run:

   ```
   scripts/dispatch.py <url>
   ```

   `dispatch.py` first builds the **shared page sample** using the skill that
   declares `"provides": ["page_sample"]`, falling back to the first specialist
   that ships a `scripts/sitemap.py`. It writes the sample to the work directory
   and passes `--sample-file` to every specialist, so all skills audit the same
   pages. Without that, findings are not comparable across skills and the audit
   is not reproducible.

   It then invokes each specialist through the marketplace convention:

   ```
   skills/<specialist>/scripts/check.py <url> --sample-file <path>
   ```

   This `check.py` convention is a marketplace implementation detail, not part
   of the agentskills.io specification. Each specialist remains independently
   defined by its own `SKILL.md`. The full interface is in
   `references/specialist-contract.md`.

5. **Isolate specialist failures.** A specialist that times out, crashes, prints
   non-JSON, or omits a contract field becomes a well-formed `failure` result
   with a limitation, never an exception that ends the audit. Record the
   specialist id, failure type, error detail and affected checks.

   Never convert a failed check into a confirmed defect, and never into a claim
   that no defect exists.

6. **Collect results.** Each specialist returns:

   ```json
   {
     "skill": "specialist-skill-id",
     "status": "success",
     "observations": [],
     "findings": [],
     "limitations": [],
     "proactive_recommendations": []
   }
   ```

7. **Merge.** Pass the aggregate to `scripts/merge.py`, which performs the four
   composition steps no specialist can do alone:

   - **Deduplicate.** Findings sharing a category, funnel stage and affected
     surface are one defect. Evidence is unioned, the wider `severity_inputs`
     win, the weaker `confidence` governs.
   - **Gate.** A blocker at the `reachable` stage suppresses downstream findings
     it covers. Reach depends on the blocker's kind: a **hard access failure**
     (403, 5xx, TLS, timeout) gates everything, because nothing could be
     measured; a **policy block** (a robots.txt disallow) gates only the
     discoverability chain, because the page is still served to visitors and its
     engagement defects remain directly observable. Suppressed items move to
     `suppressed_findings` with a `blocked_by` reference; nothing is silently
     dropped.
   - **Score.** Severity is computed, never chosen:
     `score = stage_block x fact_criticality x blast_radius`, banded per
     `references/severity-model.md`. Specialists emit the three integers only. A
     specialist that supplies a severity label is in breach of the contract.
     `confidence: medium` caps severity at `high`; `confidence: low` is not
     permitted on a finding and is recorded as a limitation instead.
   - **Order and number.** Sort by severity descending, then `stage_block`
     descending, then category, then first affected URL, then title, all
     lexicographic. Number `F001`, `F002`, and so on afterwards.

8. **Finding IDs.** Assigned only after deduplication and gating, from the
   canonical order above. Deterministic for identical inputs. Never reuse a
   specialist's `local_id`. Never generate a random or timestamp-based ID.

9. **Validate.** Pass the merged report to `scripts/validate_report.py`, which
   checks it against `references/schema.json` and additionally enforces the
   invariants JSON Schema cannot express: unique and contiguous IDs from `F001`,
   summary counts equal to the actual severity distribution, canonical ordering,
   every severity equal to the model result for its own `severity_inputs`, every
   `blocked_by` resolving, and no finding at low confidence.

   Correct a validation failure only where the correction is deterministic.
   Otherwise record the failure as an audit limitation rather than inventing
   data.

10. **Emit exactly one JSON object.** The report shape is defined by
    `references/schema.json`. It contains, at minimum:

    - `site`, `audited_at`, `summary`, `findings`;
    - `audit` — marketplace name and version, skills run, the shared sample and
      its strategy, duration;
    - `findings[]` — `id`, `title`, `severity`, `evidence[]`, and
      `suggested_action` as an **object** with `summary` and `priority`, plus
      optional `rationale`, `effort` and `verify_by`; each finding also carries
      `stage`, `category`, `confidence`, `scope`, `affected_urls` and
      `severity_inputs`;
    - `suppressed_findings` — gated items with their blocker;
    - `limitations` — every check that could not be completed;
    - `proactive_recommendations` — improvements offered where no defect was
      found, deduplicated and ordered by priority.

    `audited_at` is stamped once, here. Specialists never emit a timestamp.

    Emit only the validated report. Do not expose intermediate specialist
    results. Do not invent evidence, findings or successful checks. Surface
    internal dispatch errors only as limitations.

## Reading the report

`summary.headline` names the earliest blocking stage in one sentence, because
fix order follows the funnel: repairing stage *n* is worthless while stage *n-1*
blocks. `suggested_action.priority` encodes that, promoting any finding that
blocks others one level above its severity band.

A non-zero `summary.limitations_count` means the report is **incomplete**, not
that the site is clean.

## Bundled resources

| Path | Purpose |
|---|---|
| `scripts/dispatch.py` | manifest-driven specialist execution, shared sampling, fault isolation |
| `scripts/merge.py` | dedup, funnel gating, severity scoring, canonical ordering |
| `scripts/validate_report.py` | schema conformance plus internal invariants; no dependency required |
| `references/schema.json` | the canonical report schema |
| `references/severity-model.md` | the severity arithmetic, its tables and its refusals |
| `references/specialist-contract.md` | the interface every specialist implements |

## Safety and operational guardrails

- Recommend-only. No skill in this marketplace alters a live site.
- Read-only, GET only, no authentication, no bypassing robots.txt, CAPTCHA, WAF
  or paywall.
- Bounded requests, bytes, redirects and time; polite delays; robots.txt
  respected before any retrieval.
- Deterministic: identical inputs produce a byte-identical report.
- Self-contained: no external service is needed to resolve or run the
  marketplace.
