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

The skill may use the bundled scripts in `scripts/` to perform deterministic
read-only checks.

For a target URL:

1. Run `scripts/robots.py <url>` before attempting page crawling.
2. Use its structured JSON result to determine whether crawling may proceed.
3. If crawling is permitted, run `scripts/fetch.py <url>`.
4. If rendering is required, run `scripts/render.py <url>`.
5. Use `scripts/sitemap.py <url> --emit-sample` to produce the shared page
   sample the whole marketplace audits. This skill declares
   `"provides": ["page_sample"]` in the manifest and is the sample producer.
6. Use `scripts/check.py <url> --sample-file PATH` to combine the crawl,
   retrieval, rendering, user-agent differential and machine-readability
   evidence into the specialist result. Every scope claim is bounded by the
   sample; `blast_radius` follows the affected-to-sampled ratio and is never
   inferred from a single page.

`scripts/render.py` is optional. It probes for a headless browser and reports
`available: false` when none exists, in which case client-side dependence is
inferred from hydration markers and reported at medium confidence rather than
asserted.

Scripts must remain read-only, bounded, and must not bypass robots.txt,
authentication, CAPTCHA, WAF, paywalls, or other access restrictions.

## Procedure

1. Normalize and validate the target URL.

2. Check the applicable `robots.txt` rules before crawling.

   - Respect `robots.txt`.
   - Do not bypass a disallow rule.
   - Record when a requested check cannot be performed because crawling is disallowed, including the applicable robots.txt rule when it can be determined.

3. Discover available sitemap resources.

   - Check for `Sitemap:` declarations in `robots.txt`.
   - Check the conventional `/sitemap.xml` location when permitted.
   - Follow sitemap indexes only within the audit's bounded crawl limits.
   - Record discovered sitemap URLs and the number of URLs available for inspection where determinable.
   - Use sitemap URLs to identify representative pages for bounded auditing when appropriate.
   - Do not treat the absence of a sitemap as a defect by itself.
   - Do not crawl an unbounded number of sitemap URLs.

4. Perform permitted HTTP retrieval.

   Record deterministic observations including, where applicable:

   - DNS/connectivity result;
   - TLS/SSL result;
   - HTTP status;
   - response URL;
   - redirect chain;
   - request duration;
   - timeout;
   - response content type;
   - whether usable HTML was returned.

5. Classify access failures by observed behavior.

   Possible observations include:

   - HTTP 403 or other access-denied responses;
   - authentication requirements;
   - CAPTCHA or anti-bot challenges;
   - paywall or partial-content barriers;
   - environment-specific geographic or network restrictions;
   - redirect loops or excessive redirect chains;
   - connection or TLS failures;
   - request timeouts.

   Do not claim a specific underlying cause such as WAF, bot protection, IP restriction, or server policy unless the available evidence supports that conclusion.

6. Preserve the initial HTML response when retrieval succeeds.

7. Extract machine-readable content from the initial response.

   Examine, where applicable:

   - readable text;
   - page title and metadata;
   - headings;
   - links and navigational paths;
   - structured data such as JSON-LD;
   - important factual content exposed directly in the HTML.

   Treat these as observations and evidence. Do not classify the absence of any single element as a defect without considering whether the missing element materially affects machine extraction of important information.

8. Render the page when permitted and when rendering is useful for determining whether important content depends on client-side execution.

9. Compare the initial HTML with the rendered page.

   - Identify meaningful differences in machine-readable content.
   - Determine whether important factual content is available only after rendering.
   - Do not treat JavaScript usage itself as a defect.
   - Record measurable differences where possible.

10. Check important factual content for non-textual exposure.

   - Identify important facts represented through images, video, canvas, graphical text, or other non-textual components.
   - Determine whether an equivalent, clearly readable representation is available in the inspected HTML or rendered text.
   - Do not treat the presence of non-textual content itself as a defect.
   - Create a finding only when evidence indicates that important information lacks an accessible machine-readable representation.

11. Produce a specialist result.

   The result must distinguish:

   - confirmed findings;
   - observations that do not meet the threshold for a finding;
   - audit limitations caused by failed or unavailable checks.

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

- access and retrieval;
- crawl permission;
- HTTP behavior;
- redirects;
- TLS/connectivity;
- timeout handling;
- raw HTML inspection;
- permitted browser rendering;
- initial-versus-rendered content comparison;
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
- Do not perform rate-abusive crawling.
- Keep crawling bounded and relevant to the audit.
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

