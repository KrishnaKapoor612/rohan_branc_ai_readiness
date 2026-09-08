---
name: freshness-corroboration-audit
description: Audits whether an assistant can tell which real-world entity a site belongs to, how old its claims are, and whether anything outside the site agrees with them. Detects missing Organization or equivalent identity markup, an identity with no sameAs links to external records, a site that states more than one name for itself, content with no date signal so a current fact cannot outrank a stale one, and declared sameAs profiles that do not resolve. Use when diagnosing why an assistant repeats outdated facts about a brand, confuses it with a different entity of the same name, or describes it in someone else's words.
license: Apache-2.0
allowed-tools: Bash
compatibility: Requires a skills-compatible agent runtime able to execute the bundled Python 3.9+ scripts. Standard library only, no third-party packages, no browser.
---

# Freshness and Corroboration Audit

## When to use

Use this skill for the trust layer of discoverability. A page can be perfectly
reachable, rendered and extractable and still lose, because a machine cannot
tell which entity it describes, cannot tell when the claim was true, and finds
the claim contradicted or unsupported elsewhere.

Invoked by the marketplace entrypoint, `audit-orchestrator`. Can also run
standalone.

## The three failures it separates

| Stage | Question | Failure |
|---|---|---|
| `unambiguous` | which real-world thing is this? | name collides with another entity; nothing distinguishes them |
| `current` | when was this true? | the claim carries no date, so recency cannot favour it |
| `corroborated` | does anything else say the same? | the claim exists in one place only, or its external references are dead |

The freshness failure is the least intuitive and the most damaging. Updating a
page does not delete the older version of the fact from the rest of the web, and
the old version usually has more copies. When the current statement is undated
and the stale one is dated, recency cannot be used to prefer the current one, so
the outdated version keeps being repeated.

## Inputs

Required:

- `url` — public website URL or bare domain.

Optional:

- `--sample-file` — the shared page sample. Bounds every scope claim.
- `--no-external` — skip verification of declared `sameAs` targets.
- `--user-agent`, `--timeout`, `--budget-ms`.

## Execution

`scripts/check.py <url> [--sample-file PATH] [--no-external]`

## Procedure

1. Load the shared page sample; record a limitation if none was supplied.
2. Retrieve each sampled page once, polite delay between requests.
3. Flatten every JSON-LD block on the page, expanding `@graph`, skipping blocks
   that fail to parse.
4. Identify nodes typed `Organization`, `Corporation`, `LocalBusiness`,
   `OnlineStore`, `Store`, `Brand`, `WebSite`, `NGO` or
   `EducationalOrganization`.
5. Collect the declared `name` values, the `sameAs` array, and `og:site_name`.
6. Look for any date signal: `dateModified`, `datePublished`, `dateCreated`,
   `uploadDate`, a `time[datetime]` element, or article modified-time metadata.
7. Emit findings against the thresholds below.
8. Unless disabled, fetch up to 3 declared `sameAs` targets and check only that
   they resolve. Report at medium confidence, because one environment being
   blocked is not proof the profile is gone.
9. Emit proactive recommendations.

## Detection thresholds

| Finding | Fires when |
|---|---|
| `FC-001` no declared identity | no sampled page carries an identity-typed JSON-LD node |
| `FC-002` no sameAs graph | an identity node exists but no `sameAs` value anywhere in the sample |
| `FC-003` conflicting self-names | more than one distinct name across identity `name` and `og:site_name` |
| `FC-004` undated content | a sampled page exposes no date signal at all |
| `FC-005` dead sameAs target | a checked `sameAs` URL returns 404 or 410 |

`FC-001` and `FC-002` are mutually exclusive by construction: a site cannot lack
an identity node and also have one without `sameAs`. This keeps one root cause
from producing two findings.

## What this skill deliberately does not claim

It does not query the open web for third-party descriptions of the brand, and it
does not assert that any external source contradicts the site. Doing so would
require a search backend, which would make the marketplace dependent on an
external service and non-reproducible. That gap is stated explicitly as a
limitation in every report, so a reader never mistakes silence for agreement.

What it can establish without a search backend is whether the site has made
corroboration **possible**: a declared identity, resolvable external references,
one consistent name, and dated claims. Those are the preconditions. The
proactive recommendations cover the rest.

## Evidence rules

Ratios over sampled pages, named example URLs, measured values rather than
inferences. External checks are reported at medium confidence and always paired
with a limitation naming what was not assessed.

## Scope boundaries

Owns entity identity, freshness signals and corroboration readiness.

Does not own crawl permission, retrieval, rendering or extraction structure
(`crawl-render-audit`), nor post-arrival orientation (`engagement-audit`).
Missing JSON-LD *as a structured-data defect* belongs to `crawl-render-audit`;
this skill only asks whether an **identity** is declared, which is a different
question about the same markup.

## Safety and operational guardrails

- Read-only, GET only, TLS verified, bounded bytes and redirects.
- Off-origin requests are limited to at most 3 public `sameAs` URLs the site
  itself declares, which is the corroboration exception in the specialist
  contract. Nothing else off-origin is fetched.
- Never authenticates, never bypasses an access control.
- No third-party packages.

## Output

One JSON object on stdout, per the marketplace specialist contract:

```json
{
  "skill": "freshness-corroboration-audit",
  "status": "success",
  "observations": [],
  "findings": [],
  "limitations": [],
  "proactive_recommendations": []
}
```
