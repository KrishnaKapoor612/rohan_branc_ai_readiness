# brand-ai-readiness-audit

An Agent Skill Marketplace that audits any website for **AI discoverability** (getting found and cited by assistants) and **on-site engagement** (keeping the visitor once they arrive), then emits one deterministic, evidence-backed report of findings plus prioritized fixes.

**Recommend-only.** Nothing in this marketplace modifies a live site.

## Quick start

```bash
python3 skills/audit-orchestrator/scripts/dispatch.py https://example.com \
  | python3 skills/audit-orchestrator/scripts/merge.py - \
      --audited-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  | python3 skills/audit-orchestrator/scripts/validate_report.py -
```

Requirements:

- Python 3.9+
- Standard library only
- No third-party packages
- No bundled browser
- No external service required

## Architecture

The marketplace follows a **"collector collects, specialists reason"** architecture.

The entrypoint performs bounded, read-only network collection once and produces shared evidence. Specialist skills consume that evidence and perform independent analysis without making network requests.

```text
Website URL
    │
    ▼
┌─────────────────────────────┐
│ audit-orchestrator          │
│ entrypoint / composition    │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│ evidence.py                 │
│ shared bounded collection   │
│ fetch + robots + sitemap    │
│ page evidence + UA probe    │
└─────────────┬───────────────┘
              │
              │ evidence.json
              │
       ┌──────┼──────────┐
       ▼      ▼          ▼
   crawl-   freshness-  engagement-
   render   corroboration audit
     │          │          │
     └──────────┼──────────┘
                ▼
          merge.py
                │
                ▼
       validate_report.py
                │
                ▼
          Final JSON report
```

This decomposition reflects the marketplace requirement for one designated entrypoint composing multiple focused skills. Each specialist has a distinct concern rather than duplicating the collection workflow.

## The audit funnel

Discovery is treated as a funnel rather than a single pass:

```text
exists
  → reachable
  → renderable
  → extractable
  → unambiguous
  → corroborated
  → current
  → engaging
  → actionable
```

Each specialist owns a contiguous analytical area:

| Skill | Responsibility | Question |
|---|---|---|
| `crawl-render-audit` | Access, rendering and extraction | Can a machine reach the page, read it, and extract useful facts? |
| `freshness-corroboration-audit` | Identity, freshness and corroboration | Is the entity clear, are claims time-aware, and are declared external references usable? |
| `engagement-audit` | On-site engagement | Once a visitor arrives, can they orient themselves and find what they need? |
| `audit-orchestrator` | Collection and composition | How should the specialist results be combined, gated, scored and reported? |

A defect is attributed to the earliest stage that actually prevents the later measurement.

Downstream findings that cannot be independently verified because of an upstream blocker are gated rather than reported as separate defects. Suppressed findings remain visible in the report with their blocker reference.

## What each skill does

### `audit-orchestrator` — entrypoint

The entrypoint owns the audit workflow and final composition.

- `scripts/evidence.py` performs the shared bounded network collection and builds canonical evidence.
- `scripts/fetch.py` provides the low-level HTTP retrieval used by the collector.
- `scripts/sitemap.py` discovers the bounded page sample.
- `scripts/dispatch.py` runs the collector once and invokes each specialist against the same evidence.
- `scripts/merge.py` deduplicates, gates, scores and numbers findings.
- `scripts/validate_report.py` validates the final report schema and invariants.
- `references/` contains the report schema, severity model and specialist contract.

Specialists are isolated from the network collection layer. They receive the shared evidence through `--evidence-file`.

### `crawl-render-audit`

Owns machine accessibility, rendering and extraction analysis.

It evaluates:

- `robots.txt` accessibility decisions
- HTTP retrieval failures
- renderability and client-side dependence
- machine-readable page content
- structured data and extraction signals
- crawl/sample coverage
- browser-vs-assistant user-agent differential evidence

`check.py` analyzes the evidence produced by the orchestrator. It does not perform its own network retrieval.

### `freshness-corroboration-audit`

Owns identity, freshness and corroboration analysis.

It evaluates:

- declared identity
- JSON-LD identity signals
- naming consistency
- date/freshness signals
- declared `sameAs` references
- bounded `sameAs` verification results already collected by `evidence.py`

The specialist does **not** independently fetch `sameAs` URLs. It consumes the shared verification results from:

```text
evidence.site_level.corroboration.same_as_checked
```

### `engagement-audit`

Owns post-arrival engagement analysis.

It evaluates signals such as:

- deep-link anchors
- content anchors
- search recovery paths
- crawlable navigation
- breadcrumbs
- blocking interstitial patterns
- orientation/context signals

Like the other specialists, it analyzes shared evidence rather than performing network collection.

## How composition works

All specialists return the same contract shape:

```json
{
  "skill": "...",
  "status": "...",
  "observations": [],
  "findings": [],
  "limitations": [],
  "proactive_recommendations": []
}
```

The orchestrator then combines those results.

### Collection

The shared collector gathers the bounded evidence once. Specialists receive the same evidence so their results are based on the same sampled pages and retrieval outcomes.

### Deduplication

`merge.py` combines findings that represent the same underlying defect. Evidence and severity inputs are merged according to the severity model.

### Gating

Upstream blockers can suppress downstream findings that could not be independently verified.

Hard access failures can block downstream analysis because the page could not be measured.

A `robots.txt` policy block is treated differently: the page may still be available to human visitors, so directly observable engagement problems are not automatically suppressed.

Suppressed findings are retained in `suppressed_findings` with a `blocked_by` reference.

### Severity

Specialists provide severity inputs rather than choosing the final severity.

```text
score = stage_block × fact_criticality × blast_radius
```

The orchestrator converts the score into:

```text
critical >= 27
high     >= 14
medium   >= 6
low      < 6
```

Confidence can further cap the resulting severity. The complete model is defined in:

```text
skills/audit-orchestrator/references/severity-model.md
```

### Deterministic ordering

Final findings are sorted deterministically and numbered after merging. Identical evidence produces the same ordering and severity calculations.

## Shared evidence and network access

There is **one collection layer**.

```text
audit-orchestrator
       │
       ▼
   evidence.py
       │
       ├── fetch.py
       ├── robots.py
       ├── sitemap.py
       └── bounded collection
              │
              ▼
         evidence.json
              │
       ┌──────┼──────┐
       ▼      ▼      ▼
    specialist check.py files
```

`fetch.py` is **not duplicated into the specialist folders**.

Each specialist is a standard-library-only Python module that receives the canonical evidence through its CLI `--evidence-file` argument. Specialists have no cross-folder Python imports and do not make network requests.

This keeps network access centralized and makes the separation between collection and reasoning explicit.

## Sampling

The collector uses a bounded page sample rather than attempting to crawl an entire website.

Where available, sitemap discovery provides candidate URLs. When sitemap discovery is unavailable and crawling is permitted, the collector can use bounded same-origin link discovery from the already retrieved homepage.

When crawling is disallowed, the safe fallback is homepage-only analysis.

The same shared page evidence is passed to every specialist, making their measurements comparable and reproducible.

## Limitations are first-class

A check that could not run is neither automatically a defect nor a pass.

Incomplete checks are recorded in `limitations`.

For example:

- rendering may be unavailable when no supported browser exists;
- external corroboration may be limited to declared `sameAs` references;
- a blocked or failed page may prevent downstream verification.

A non-zero `limitations_count` means the audit is incomplete; it does **not** mean the website is clean.

## No external search backend

The marketplace does not perform open-web search for third-party contradictions.

External corroboration is limited to evidence the site itself declares, such as bounded `sameAs` references. The audit can determine whether those declared references resolve, but it does not claim that the wider web agrees with the site's claims.

This keeps the audit deterministic and avoids a dependency on an external search service.

## False-positive controls

The checks are intentionally conservative.

Examples:

- a missing sitemap is not automatically treated as a defect;
- missing rendering capability does not itself manufacture a rendering finding;
- an environment-specific access failure is not automatically generalized to the entire site;
- downstream checks are not reported when an upstream blocker prevents independent verification;
- external corroboration limitations are reported as limitations rather than fabricated agreement.

## Report shape

The final report is validated against:

```text
skills/audit-orchestrator/references/schema.json
```

The required report floor includes:

```text
site
audited_at
summary
findings[]
```

Each finding contains the required:

```text
id
title
severity
evidence
suggested_action
```

The implementation may additionally provide:

```text
audit
suppressed_findings
limitations
proactive_recommendations
stage
category
confidence
scope
affected_urls
severity_inputs
source_skill
```

## Repository layout

```text
brand-ai-readiness-audit/
├── marketplace.json
├── README.md
└── skills/
    ├── audit-orchestrator/
    │   ├── SKILL.md
    │   ├── scripts/
    │   │   ├── dispatch.py
    │   │   ├── evidence.py
    │   │   ├── fetch.py
    │   │   ├── merge.py
    │   │   ├── sitemap.py
    │   │   └── validate_report.py
    │   └── references/
    │       ├── schema.json
    │       ├── severity-model.md
    │       └── specialist-contract.md
    │
    ├── crawl-render-audit/
    │   ├── SKILL.md
    │   └── scripts/
    │       └── check.py
    │
    ├── freshness-corroboration-audit/
    │   ├── SKILL.md
    │   └── scripts/
    │       └── check.py
    │
    └── engagement-audit/
        ├── SKILL.md
        └── scripts/
            └── check.py
```

## Adding a specialist

A new specialist should:

1. Have its own `SKILL.md`.
2. Provide a `scripts/check.py`.
3. Follow `references/specialist-contract.md`.
4. Consume the shared evidence through `--evidence-file`.
5. Avoid independent network collection.
6. Be registered in `marketplace.json`.

The entrypoint should remain responsible for collection and composition.

## Safety

The marketplace is designed to be read-only and recommend-only.

- Network retrieval is bounded.
- Only permitted public web retrieval is performed.
- `robots.txt` is respected.
- No authentication is attempted.
- No CAPTCHA, WAF, paywall or access-control bypass is attempted.
- No destructive or site-altering action is performed.
- No live website is modified.
- Off-origin requests are limited to bounded `sameAs` URLs explicitly declared by the audited site.
- The audit is designed to remain within the contest's runtime and package-size constraints.
