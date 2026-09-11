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

It is invoked by the marketplace entrypoint, `audit-orchestrator`.

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

- `--evidence-file` — the shared `evidence.json` produced once by
  `audit-orchestrator/scripts/evidence.py`. Every scope claim this skill makes
  is bounded by the pages present in this file. Without it, the skill records
  a limitation and emits no findings — it does not fetch pages itself.
- `--sample-file` — deprecated, accepted only so an older orchestrator does not
  fail argument parsing. Not read.
- `--user-agent`, `--timeout`, `--budget-ms`, `--no-render`.

## Execution

`scripts/check.py <url> --evidence-file PATH`

This skill is an **analysis specialist**, not a page retriever. It consumes
the shared `evidence.json` and does not fetch pages or import `fetch.py`/
`robots.py` — those files are not bundled here and live only under
`audit-orchestrator/scripts/`. Crawl permission for the sampled pages was
already decided once, upstream, by `evidence.py` (`robots_allowed` on each
page in the evidence file); this skill treats that field as authoritative
and does not re-derive or re-check it.

## Procedure

1. Load the shared evidence file (`--evidence-file`). Record a limitation
   if none was supplied, or if it contains no usable `pages` array.
2. Read each page's `robots_allowed` field, already determined upstream.
   Do not re-check robots.txt yourself. A page marked disallowed is treated
   as excluded from analysis, not retried.
3. For each page already present in the evidence file, read the
   already-collected data rather than retrieving anything:
   - `page["headings"]` for h2/h3 headings, and how many carry an `id`;
   - `page["links"]` for distinct same-origin link counts already extracted
     from the initial HTML;
   - `page["page_structure"]` for presence of a `main`/`article` landmark
     and of an `h1`;
   - `page["engagement_signals"]["search_ui_detected"]` for presence of a
     search input the visitor could recover with;
   - breadcrumb markup or `BreadcrumbList` structured data, matched directly
     against `page["html"]` — the initial HTML already collected upstream,
     not re-fetched;
   - a modal structure, consent vocabulary, and a scroll lock, measured
     separately from `page["html"]`/`page["engagement_signals"]`;
   - words of readable text, from `page["page_structure"]`.
4. Convert measurements into findings only where the thresholds below are met.
5. Set `blast_radius` from the ratio of affected pages to sampled pages, never
   from a single page.
6. Emit `severity_inputs`, never a severity label.
7. Emit proactive recommendations that strengthen the handoff even where no
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
reports what was measured rather than what it implies. A page missing from the
evidence file, or carrying a retrieval error/limitation, never becomes a claim
about the site.

## Scope boundaries

This skill owns post-arrival orientation and context continuity.

It does not own crawl permission, retrieval, rendering, structured-data
extraction (`crawl-render-audit`), or entity identity, freshness and
corroboration (`freshness-corroboration-audit`). Where a measurement could
belong to another skill, it is emitted as an observation, not a finding, so the
orchestrator never receives one root cause twice.

## Safety and operational guardrails

- Read-only analysis only. This skill makes no network requests of any kind —
  it reads only the shared `evidence.json` already collected upstream.
- Never authenticates, never bypasses robots.txt, a consent wall, a CAPTCHA or a
  WAF. A consent wall is detected by reading markup already present in the
  supplied evidence, never by dismissing it or fetching a page to check.
- Do not independently crawl, fetch, or re-request any page already covered by
  the shared evidence; all such retrieval bounding, rate-limiting, and TLS
  verification is enforced upstream by `evidence.py`.
- No third-party packages and no network access at all.

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