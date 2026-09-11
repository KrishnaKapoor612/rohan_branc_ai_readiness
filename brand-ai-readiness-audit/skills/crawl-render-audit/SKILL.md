---
name: crawl-render-audit
description: Audits whether a public website can be reached, retrieved, rendered, and machine-read through permitted read-only access. Detects evidence-backed access failures, crawl restrictions, rendering gaps, retrieval problems, and cases where important factual content is available to humans but not clearly exposed as machine-readable text.
license: Apache-2.0
allowed-tools: Bash
compatibility: Requires a skills-compatible agent runtime capable of executing the marketplace's bundled scripts.
---

# Crawl Render Audit

## Purpose

Determine what machine-readable evidence can actually be obtained from a public website through permitted, read-only retrieval and rendering.

This skill focuses on the machine-accessibility layer of the audit:

- whether the target can be reached;
- whether crawling is permitted;
- what the server returns;
- whether the page can be retrieved within the available audit time budget;
- whether redirects, TLS failures, timeouts, challenges, or access restrictions prevent inspection;
- whether important content appears only after client-side rendering;
- whether important factual information is exposed as readable text;
- whether the origin serves different responses to different user agents,
  which is how a stated robots.txt permission can be contradicted by an edge
  rule that refuses assistant crawlers in practice;
- and what evidence could not be verified.

This skill must not modify the website or bypass access controls.

## Inputs

Required:

- `url` — public website URL or bare domain.

## Execution

This skill is an **analysis specialist**, not a page retriever. Crawling,
robots-permission checks, fetching, sampling and (optional) headless rendering
are all performed once, upstream, by `audit-orchestrator/scripts/evidence.py`,
which writes the shared `evidence.json`. This skill folder intentionally does
not bundle `robots.py`, `fetch.py`, `render.py` or `sitemap.py`. Those live
only under `audit-orchestrator/scripts/` and are invoked once, upstream, by
`audit-orchestrator/scripts/evidence.py` — see the specialist contract in
`audit-orchestrator/references/specialist-contract.md`.

For a target URL:

1. Load the shared evidence with `scripts/check.py <url> --evidence-file PATH`.
2. Reason over `evidence["site_level"]["robots"]` and `evidence["pages"][*]`
   (`robots_allowed`, `status`, `html`, `jsonld`, `page_structure`, `render`)
   to determine crawl permission, retrieval outcomes, extractability and
   render-diff findings. Do not re-fetch, re-crawl, or independently sample.
3. Every scope claim is bounded by the sampled pages in the evidence file;
   `blast_radius` follows the affected-to-sampled ratio and is never inferred
   from a single page.

Rendering, when it happened, is recorded per page under `page["render"]`
(`attempted`, `available`, `signals`). If `available` is `false`, no headless
browser was present in the collection environment, and client-side dependence
is instead inferred from hydration markers in `page_structure` and reported at
medium confidence rather than asserted. This skill never invokes rendering
itself and never attempts to install a browser binary — see "Rendering
guardrail" below.

Scripts must remain read-only, bounded, and must not bypass robots.txt,
authentication, CAPTCHA, WAF, paywalls, or other access restrictions.


## Rendering guardrail

If rendered evidence is unavailable (`page["render"]["available"]` is `false`),
treat that as a fact about the collection environment, not a problem to solve.

Do not attempt to `pip install playwright`, run `playwright install chromium`,
download a browser binary, or shell out to any installer mid-audit. Doing so
risks the marketplace's runtime budget — a Chromium download alone can exceed
the 5-minute ceiling — and risks violating the Python/Node.js-only environment
constraint this marketplace operates under.

When rendering was unavailable, report it as such and continue reasoning from
`page["page_structure"]`'s hydration markers at medium confidence, exactly as
`evidence.py`'s collection layer already does. Never assert a render-dependent
finding at high confidence when `available` is `false` — downgrade confidence
instead of inventing certainty the evidence doesn't support.

## Procedure

1. Load the shared evidence file (`--evidence-file`). Confirm it contains a
   `pages` array; if not, record a limitation and stop.

2. Read `evidence["site_level"]["robots"]` and each page's `robots_allowed`
   field. Do not re-check robots.txt yourself — reason over what was already
   determined upstream.

3. Read the sample composition already present in the evidence file. Do not
   discover or crawl sitemaps yourself; sampling was already bounded upstream
   by `evidence.py`/`sitemap.py`.

4. For each page, read the retrieval outcome already recorded: `status`,
   `final_url`, redirect info, `error`, and `limitations`. Classify access
   failures from these recorded fields — do not attempt retrieval yourself.

5. Classify access failures by the observed fields already present (HTTP
   status, error type, limitations) rather than by performing your own
   requests.

6. Read `page["html"]` — the initial HTML response is already preserved in
   the evidence file.

7. Extract machine-readable content signals from what's already computed:
   `page["jsonld"]`, `page["page_structure"]`, headings, links — do not
   re-parse HTML from scratch where evidence.py has already extracted it.

8. Read `page["render"]` (`attempted`, `available`, `signals`) for whether
   rendering happened upstream and what it found. Do not render pages
   yourself — see the Rendering guardrail above.

9. Compare `page["render"]["signals"]` fields (`server_text_chars`,
   `text_chars`, `gained_chars`) — this diff was already computed upstream;
   reason over it rather than performing your own comparison.

10. Check for important facts represented through images, video, canvas, or
    other non-textual components, using the text/structure signals already
    present in the evidence file.

11. Produce a specialist result distinguishing confirmed findings,
    observations, and limitations.

## Evidence Rules

Every confirmed finding must be supported by concrete observations from the audit.

Prefer evidence that states:

- the URL or resource inspected;
- the HTTP response or retrieval result;
- what was present or absent;
- the difference between initial and rendered content when relevant;
- measurable counts or comparisons where available.

Do not infer a website-wide condition from a single environment-specific failure.

For example:

- `HTTP 403 from the audit environment` does not prove that the website is globally inaccessible.
- `rendering failed` does not prove that the website is invisible to all AI systems.
- `JavaScript is present` does not prove a machine-readability problem.
- `an image exists` does not prove that its information is inaccessible to machines.
- `a timeout occurred` does not prove that the website does not exist.

When evidence is insufficient to confirm a defect, record the situation as an audit limitation or observation rather than inventing a finding.


## Failure Handling 

A failed check must not be converted into a successful check or a confirmed defect.

Examples:

- If HTTP retrieval times out, report the timeout and do not claim that the site is nonexistent.
- If TLS verification fails, report the TLS failure and do not disable certificate verification merely to force retrieval.
- If rendering fails, report that rendered content could not be verified.
- If a CAPTCHA or anti-bot challenge prevents inspection, report that the content could not be inspected through the permitted audit.
- If access is restricted from the audit environment, describe the observed restriction without generalizing it to all users or locations.


## Scope Boundaries

This skill owns:

- interpreting access, retrieval, and crawl-permission signals already collected upstream;
- HTTP behavior;
- redirects;
- TLS/connectivity;
- timeout handling;
- raw HTML inspection;
- interpreting rendered-vs-initial content signals already collected upstream;
- machine-readable content extraction;
- non-textual fact exposure.

This skill does not own:

- cross-web freshness or corroboration;
- entity identity resolution across external sources;
- broader off-site reputation analysis;
- visitor engagement or conversion analysis.

Those concerns belong to other specialist skills in the marketplace.

## Safety and Operational Guardrails

- Read-only operation only.
- Never modify website content or configuration.
- Never authenticate to protected areas.
- Never bypass CAPTCHA, WAF, access controls, paywalls, robots.txt, or other restrictions.
- Do not attempt independent retrieval, crawling, or rendering; all bounding and rate-limiting of live requests is enforced upstream by `evidence.py`. This skill only interprets already-collected evidence.
- Respect the marketplace runtime budget.
- Do not require an external service to interpret the marketplace.

## Output

Return one structured JSON object for the orchestrator:

```json
{
  "skill": "crawl-render-audit",
  "status": "success",
  "observations": [],
  "findings": [],
  "limitations": []
}
```

