---
name: freshness-corroboration-audit
description: Audits whether a website provides clear identity signals, usable freshness signals for time-sensitive claims, and corroboration-ready information that can help AI systems distinguish the brand from similarly named entities and avoid relying on outdated information.
license: Apache-2.0
allowed-tools: Bash
compatibility: Requires a skills-compatible agent runtime able to execute the bundled Python 3.9+ scripts. Standard library only, no third-party packages, no browser.
---

# Freshness and Corroboration Audit

## When to use

Use this skill for the trust layer of AI discoverability. A page can be reachable
and readable yet still provide weak signals for entity identification, freshness,
or cross-source consistency.

Invoked by the marketplace entrypoint, `audit-orchestrator`.

## Responsibility

This skill evaluates three related dimensions:

| Dimension | Question | What counts as a meaningful problem |
|---|---|---|
| `identity` | Which real-world entity does this site represent? | Identity signals are materially conflicting or insufficient to distinguish the site from a plausible same-name entity. |
| `freshness` | When was a time-sensitive claim true? | A materially time-sensitive factual claim lacks a usable freshness signal or has conflicting temporal signals that make the current version difficult to identify. |
| `corroboration readiness` | Can the site's identity and important facts be cross-checked? | Declared external identity references are broken, or the site's own identity signals make external corroboration unnecessarily ambiguous. |

These dimensions are related but independent. A weakness in one dimension must not be
inferred merely because another dimension is weak.

## Evidence model

The orchestrator supplies the shared page sample and page-level evidence collected for
the audit. This skill is an **analysis specialist**, not a page retriever.

The shared evidence may contain, where available:

- sampled URLs and their final URLs;
- HTTP status and content type;
- retrieved HTML/text;
- JSON-LD blocks and parsed structured-data signals;
- Open Graph metadata;
- visible date elements and metadata;
- declared `sameAs` values;
- retrieval/rendering limitations relevant to interpreting those signals.

The skill must analyze the supplied evidence and must not independently re-fetch sampled
pages. This keeps all specialists on the same page set and prevents duplicate network
work.

If the shared evidence is unavailable or incomplete, record a limitation and restrict
claims to the evidence that is actually available. Never turn missing evidence into a
website defect.

## Identity analysis

Inspect machine-readable and page-level identity signals, including relevant JSON-LD
identity nodes and `og:site_name` when present.

Recognized identity types may include `Organization`, `Corporation`, `LocalBusiness`,
`OnlineStore`, `Store`, `Brand`, `WebSite`, `NGO`, and `EducationalOrganization`.

The presence or absence of one particular schema type is **not** itself a defect.
Identity analysis should consider the available signals together.

A site may legitimately use abbreviated names, legal names, trading names, or brand
names. Different strings are not automatically conflicting.

### Identity finding rule

Emit an identity finding only when the evidence demonstrates a **materially inconsistent
or ambiguous identity**, for example:

- authoritative identity signals on the site name different organizations or brands;
- the site's own identity signals materially disagree across important sampled pages;
- the available identity information is insufficient to distinguish the site from a
  plausible same-name entity and the site provides no useful disambiguating signal.

If no identity-typed JSON-LD is present but the site's visible and metadata identity is
otherwise clear, record an observation or proactive recommendation rather than a defect.

## Freshness analysis

Date signals are useful only when they help establish the currency of a claim.
Inspect signals such as:

- `dateModified`;
- `datePublished`;
- `dateCreated`;
- `uploadDate`;
- `time[datetime]`;
- article modified-time metadata;
- other explicit page-level temporal statements present in the supplied evidence.

Do **not** treat an undated page as stale merely because it has no date signal.
Homepages, contact pages, legal pages, evergreen descriptions, and other content may
legitimately have no date.

First determine whether the page contains a materially time-sensitive factual claim.
Examples can include current leadership, pricing, availability, current offerings,
current policies, locations, deadlines, or other facts whose correctness depends on
time. Then evaluate whether the evidence contains a usable freshness signal for that
claim.

### Freshness finding rule

Emit a freshness finding only when:

1. the supplied evidence identifies a materially time-sensitive factual claim; and
2. the claim has no usable freshness signal, or the available temporal signals are
   materially inconsistent; and
3. the limitation is not simply caused by unavailable retrieval/rendering evidence.

Absence of a date on an otherwise evergreen page is not a finding.

## Corroboration-readiness analysis

This skill does not perform unrestricted web search and does not claim that external
sources agree or disagree with the site unless such evidence is explicitly supplied by
the marketplace.

Inspect the site's own declared identity links, especially `sameAs`, when present.
Declared external references can strengthen identity disambiguation and provide useful
starting points for cross-source verification.

The absence of `sameAs` is **not** by itself a defect. It should normally produce an
observation or proactive recommendation when useful.

If the site declares an external identity reference and the shared evidence includes a
bounded verification result showing that the target returned HTTP 404 or 410, that is a
valid finding. Other network failures are not proof that the external profile is gone.

When external verification is enabled by the marketplace, at most three public `sameAs`
targets may be checked. Those checks are limited to determining whether the declared
reference resolves; they do not establish that the external source agrees with the
site's claims.

## Detection thresholds

| Finding | Fires when |
|---|---|
| `FC-001` conflicting identity signals | Supplied evidence shows materially conflicting identity signals that could cause entity confusion. |
| `FC-002` time-sensitive claim lacks usable freshness | A materially time-sensitive factual claim lacks a usable freshness signal, or its available temporal signals materially conflict. |
| `FC-003` dead declared identity reference | A checked public `sameAs` target returns HTTP 404 or 410. |

Do not emit a finding solely because:

- Organization JSON-LD is absent;
- `sameAs` is absent;
- a page has no date;
- a page lacks a particular metadata field;
- two equivalent brand/legal-name forms differ textually;
- a third-party source was not searched;
- evidence could not be retrieved or rendered.

These conditions may instead become observations, limitations, or proactive
recommendations when they are useful to the audit.

## Procedure

1. Load the shared page/evidence sample supplied by the orchestrator.
2. Verify that the evidence is bounded to the shared sample; do not select additional
   pages independently.
3. For each usable sampled page, inspect the supplied identity, temporal, and
   `sameAs` evidence.
4. Evaluate identity signals together rather than treating any single missing field as
   a failure.
5. Identify materially time-sensitive claims before evaluating freshness.
6. Evaluate whether those claims have usable and internally consistent freshness
   signals.
7. Evaluate declared `sameAs` references using supplied verification evidence, when
   available. Do not perform unrestricted external discovery.
8. Emit only evidence-backed findings. Put non-defect observations in `observations`
   and incomplete checks in `limitations`.
9. Emit proactive recommendations only when they would materially strengthen identity,
   freshness, or corroboration readiness.
10. Sort findings and other deterministic arrays according to the specialist contract.

## What this skill deliberately does not claim

It does not claim that a site is invisible to AI assistants merely because a particular
markup element is missing.

It does not claim that undated content is stale.

It does not claim that the absence of `sameAs` means identity ambiguity.

It does not query the open web for arbitrary third-party descriptions, and it does not
assert that an external source contradicts the site without explicit supporting
evidence. This keeps the marketplace portable and avoids turning this specialist into
an unrestricted search agent.

Without external-source evidence, this skill can establish **corroboration readiness**:
clear identity signals, usable freshness signals for time-sensitive claims, and bounded
validation of declared external identity references. It cannot establish broad
third-party agreement.

## Evidence rules

- Use measured values and named example URLs where available.
- Bound ratios and counts to the supplied sample.
- Distinguish observed evidence from interpretation.
- Do not use absence of evidence as evidence of absence.
- Every finding must include evidence sufficient for another reviewer to understand why
  the condition was detected.
- External reference checks are medium confidence unless the supplied evidence provides
  stronger, direct confirmation.

## Scope boundaries

Owns:

- entity identity interpretation;
- identity disambiguation signals;
- freshness signals for materially time-sensitive claims;
- declared external identity-reference validation;
- corroboration readiness.

Does not own:

- crawl permission or `robots.txt` decisions;
- URL discovery or page sampling;
- page retrieval/network collection;
- browser rendering;
- extraction/readability infrastructure;
- structured-data implementation as a general defect;
- post-arrival orientation or on-site engagement.

A missing or malformed JSON-LD implementation may be relevant evidence here, but a
structured-data defect itself belongs to `crawl-render-audit`. This skill evaluates what
the available identity/freshness evidence means for discoverability.

## Safety and operational guardrails

- Read-only analysis.
- Do not authenticate, submit forms, mutate state, bypass access controls, or disable
  TLS verification.
- Do not independently crawl or fetch arbitrary pages.
- External requests, if any, are limited to at most three public `sameAs` targets that
  the site itself declares and are subject to the marketplace specialist contract.
- Respect the orchestrator's bounded runtime and evidence scope.
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
