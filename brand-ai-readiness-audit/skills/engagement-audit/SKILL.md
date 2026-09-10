---
name: engagement-audit
description: Audits what happens after a visitor arrives from an AI assistant's answer, when they already know what they want and the site does not. Detects loss of the arriving context:no deep-linkable anchor for the answer, consent interstitials that block content, pages with no h1 or main landmark to orient against, no on-site search recovery path, navigation that exists only after client-side execution, and missing breadcrumbs on deep pages. Use when diagnosing why visitors who do reach a site bounce within seconds, or why an assistant can only link a site's home page instead of the section that answers the question.
license: Apache-2.0
allowed-tools: Bash
compatibility: Requires a skills-compatible agent runtime able to execute the bundled Python 3.9+ scripts. Standard library only, no third-party packages, no browser.
---

# Engagement Audit

## When to use

Use this skill for the on-site half of the brand AI-readiness problem: the
visitor who arrives mid-journey, primed by an assistant's summary, and leaves
within seconds without engaging.

It is invoked by the marketplace entrypoint, `audit-orchestrator`. It can also
run standalone against a single URL.

## The failure it measures

An assistant answers a question, names the brand, and the visitor clicks
through. At that moment the assistant knows the intent and the site does not.
Nothing carries the question across the gap:

- the assistant has no anchor to link to, so it links the page top;
- the visitor lands above the answer and has to hunt for it;
- a consent layer spends the few seconds of patience they brought;
- the page states no h1, so nothing confirms they are in the right place;
- there is no search box to recover with, and no crawlable links onward.

That is a **context handoff loss**. Every check below measures one link in it.

## Inputs

Required:

- `url` — public website URL or bare domain.

Optional:

- `--sample-file` — the shared page sample from the orchestrator. Every scope
  claim this skill makes is bounded by this list. Without it only the target URL
  is audited and a limitation is recorded.
- `--user-agent`, `--timeout`, `--budget-ms`, `--no-render`.

## Execution

`scripts/check.py <url> [--sample-file PATH]`

`scripts/fetch.py` is bundled so the skill folder is self-contained and
independently valid. If `robots.py` is present it is consulted first; otherwise
crawl permission is enforced upstream by `crawl-render-audit` and by the shared
sample, and that is recorded as an observation rather than assumed.

## Procedure

1. Load the shared page sample. Record a limitation if none was supplied.
2. Confirm crawl permission where a robots inspector is available. If the target
   is disallowed, do not retrieve, and record a limitation.
3. Retrieve each sampled page once, with a polite delay between requests.
4. For each page, measure only observable structure:
   - h2/h3 headings, and how many carry an `id`;
   - distinct same-origin links present in the initial HTML;
   - presence of a `main`/`article` landmark and of an `h1`;
   - presence of a search input the visitor could recover with;
   - breadcrumb markup or `BreadcrumbList` structured data;
   - a modal structure, consent vocabulary, and a scroll lock, measured
     separately;
   - words of readable text.
5. Convert measurements into findings only where the thresholds below are met.
6. Set `blast_radius` from the ratio of affected pages to sampled pages, never
   from a single page.
7. Emit `severity_inputs`, never a severity label.
8. Emit proactive recommendations that strengthen the handoff even where no
   defect was found.

## Detection thresholds

| Finding | Fires when | Deliberately does not fire when |
|---|---|---|
| `EN-001` no anchored headings | every sampled page that has h2/h3 headings has zero `id` attributes on them | some pages are anchored; partial coverage is an observation |
| `EN-002` blocking interstitial | a modal structure **and** consent vocabulary **and** a scroll lock appear on the same page | a dismissible banner with no scroll lock; that is recorded as an observation |
| `EN-003` no content anchor | a page has neither an `h1` nor a `main`/`article` landmark | either one is present |
| `EN-004` no search recovery | no sampled page exposes a search input, and at least 2 pages were sampled | a single-page sample, which cannot support the claim |
| `EN-005` no crawlable navigation | a page exposes fewer than 5 distinct same-origin links in the server HTML | the page links onward normally |
| `EN-006` no breadcrumbs | at least 2 sampled pages sit at depth 2 or deeper and none carry breadcrumb markup | shallow sites, where breadcrumbs add nothing |

Thin server HTML is **never** a finding here. It usually indicates client-side
rendering, which `crawl-render-audit` owns. It is recorded as an observation and
left to that skill.

## Evidence rules

Every finding states a ratio over the sampled pages, names an example URL, and
reports what was measured rather than what it implies. A check that could not
run becomes a limitation. A single unretrievable page never becomes a claim
about the site.

## Scope boundaries

This skill owns post-arrival orientation and context continuity.

It does not own crawl permission, retrieval, rendering, structured-data
extraction (`crawl-render-audit`), or entity identity, freshness and
corroboration (`freshness-corroboration-audit`). Where a measurement could
belong to another skill, it is emitted as an observation, not a finding, so the
orchestrator never receives one root cause twice.

## Safety and operational guardrails

- Read-only. GET only, no form submission, no clicks, no state change.
- Never authenticates, never bypasses robots.txt, a consent wall, a CAPTCHA or a
  WAF. A consent wall is detected by reading the markup, never by dismissing it.
- One request per sampled page, polite delay between requests, bounded bytes,
  bounded redirects, TLS verified.
- No third-party packages and no network access outside the target origin.

## Output

One JSON object on stdout, per the marketplace specialist contract:

```json
{
  "skill": "engagement-audit",
  "status": "success",
  "observations": [],
  "findings": [],
  "limitations": [],
  "proactive_recommendations": []
}
```
