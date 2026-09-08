# brand-ai-readiness-audit

An Agent Skill Marketplace that audits any website for **AI discoverability**
(getting found and cited by assistants) and **on-site engagement** (keeping the
visitor once they arrive), then emits one deterministic, evidence-backed report
of findings plus prioritised fixes.

Recommend-only. Nothing in this marketplace modifies a live site.

## Quick start

```bash
python3 skills/audit-orchestrator/scripts/dispatch.py https://example.com \
  | python3 skills/audit-orchestrator/scripts/merge.py - --audited-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  | python3 skills/audit-orchestrator/scripts/validate_report.py -
```

Python 3.9+. Standard library only. No third-party packages, no browser, no
external service.

## The idea: audit the funnel, not a checklist

Discovery is a funnel, not a switch. A page can pass one stage and fail the
next:

```
exists -> reachable -> renderable -> extractable -> unambiguous
       -> corroborated -> current -> engaging -> actionable
```

Every skill owns a **contiguous band** of that funnel. That is what makes the
decomposition a separation of concerns rather than padding:

| Skill | Band | Question it answers |
|---|---|---|
| `crawl-render-audit` | reachable, renderable, extractable | Can a machine get in, read the page, and pick out a specific fact? |
| `freshness-corroboration-audit` | unambiguous, corroborated, current | Is it clear which entity this is, when the claim was true, and does anything outside the site agree? |
| `engagement-audit` | engaging | Once a visitor arrives, can they orient and find what they came for? |
| `audit-orchestrator` | composition | Which of those failures are independent, how bad is each, and in what order should they be fixed? |

Two consequences drive the whole design:

1. **A defect belongs to the earliest stage it breaks.** A price missing because
   `/products/` is disallowed is a *reachability* defect, not an extraction
   defect.
2. **Downstream findings behind an upstream blocker are not independent
   defects.** Reporting them separately turns one root cause into four findings
   and inflates the severity counts. The orchestrator gates them instead.

## What each skill does

### `audit-orchestrator` (entrypoint)

Composes everything. It never checks a website itself.

- `scripts/dispatch.py` reads `marketplace.json`, builds the shared page sample,
  runs every specialist through the contract, and isolates failures. A
  specialist that crashes, times out, prints non-JSON or omits a contract field
  becomes a well-formed `failure` result with a limitation, never an exception
  that ends the audit.
- `scripts/merge.py` deduplicates, gates, scores and numbers.
- `scripts/validate_report.py` enforces the schema plus the invariants JSON
  Schema cannot express.
- `references/` holds the report schema, the severity model and the specialist
  contract.

### `crawl-render-audit`

Owns access and extraction. `robots.py` parses `robots.txt` per RFC 9309,
preserves the matching rule and line number as evidence, and separates three
outcomes that are usually conflated: allowed, disallowed, and *unknown* (a 5xx
or network failure, after which crawling must not proceed). `fetch.py` performs
bounded read-only retrieval and runs the user-agent differential probe.
`sitemap.py` produces the shared sample. `render.py` is optional and honest
about its own absence. `check.py` coordinates them.

### `freshness-corroboration-audit`

Owns trust. Declared identity, `sameAs` graph, self-consistent naming, date
signals, and bounded verification that declared external profiles resolve.

### `engagement-audit`

Owns the post-arrival handoff. Deep-link anchors, blocking interstitials,
content anchors, search recovery paths, crawlable navigation, breadcrumbs.

## How composition actually works

Specialists share a data contract, not prose. Each emits:

```json
{ "skill": "...", "status": "...", "observations": [],
  "findings": [], "limitations": [], "proactive_recommendations": [] }
```

The orchestrator then does four things no specialist can do alone.

**Deduplicate.** Findings sharing a category, funnel stage and affected surface
are one defect. Evidence is unioned, the wider `severity_inputs` win, the weaker
`confidence` governs.

**Gate.** Blocker reach depends on the blocker's kind. A **hard access failure**
(403, 5xx, TLS, timeout) gates everything downstream, because nothing could be
measured. A **policy block** (a `robots.txt` disallow) gates only the
discoverability chain: the page is still served to human visitors, so its
engagement defects remain directly observable and must still be reported.
Suppressed items move to `suppressed_findings` with a `blocked_by` reference.
Nothing is silently dropped.

**Score.** Severity is computed, never chosen:

```
score = stage_block x fact_criticality x blast_radius     (1..36)
critical >= 27   high >= 14   medium >= 6   low < 6
```

Specialists emit the three integers only. `confidence: medium` caps severity at
`high`. `confidence: low` is not permitted on a finding and becomes a
limitation. The full tables are in
`skills/audit-orchestrator/references/severity-model.md`.

**Order and number.** Sort by severity descending, then `stage_block`
descending, then category, then first affected URL, then title, all
lexicographic. Number `F001` onward afterwards. Identical inputs produce a
byte-identical report.

## Design decisions worth knowing about

**One shared page sample.** `sitemap.py` picks 8 to 12 pages, round-robin across
path roles so a store with 4000 product URLs does not yield a sample of nothing
but product pages. Every specialist audits that same list, which is what makes
`0 of 12 product pages` a comparable claim across skills and reproducible
between runs. `blast_radius` follows the affected-to-sampled ratio and is never
inferred from a single page.

**Limitations are first-class.** A check that did not run is neither a defect
nor a pass. Every incomplete check appears in `limitations`, and
`summary.limitations_count` being non-zero means the report is incomplete, not
that the site is clean.

**No bundled browser.** Shipping one would break the size limit, the runtime
budget and portability. `render.py` probes for a headless browser and reports
`available: false` when there is none. Client-side dependence is then inferred
from hydration markers and reported at **medium** confidence rather than
asserted. Absence of a capability never manufactures a finding.

**No search backend.** The marketplace does not query the open web to look for
third-party contradictions, because that would make it dependent on an external
service and non-reproducible. What it can establish is whether the site has made
corroboration *possible*: a declared identity, resolvable external references,
one consistent name, dated claims. The gap is stated as a limitation in every
report rather than left as implied agreement.

**False positives are designed against.** A consent banner is only a finding
when a modal structure, consent vocabulary and a scroll lock appear together. A
missing sitemap is never a defect by itself. Thin server HTML is an observation
for `engagement-audit` and a finding only for `crawl-render-audit`, which owns
rendering. An environment-specific 403 is never generalised to the whole site.

## Report shape

Schema: `skills/audit-orchestrator/references/schema.json`. It is a superset of
the required floor.

```
site, audited_at, summary, findings                 <- required floor
audit          marketplace, skills run, shared sample, duration
findings[]     id, title, severity, evidence[], suggested_action{summary, priority,
               rationale, effort, verify_by}, stage, category, confidence,
               scope, affected_urls, severity_inputs, source_skill
suppressed_findings   gated items with their blocker
limitations           every check that could not be completed
proactive_recommendations   improvements where no defect was found
```

## Layout

```
brand-ai-readiness-audit/
├── marketplace.json
├── README.md
├── UML_DIAGRAMS.md
└── skills/
    ├── audit-orchestrator/          <- entrypoint
    │   ├── SKILL.md
    │   ├── scripts/{dispatch,merge,validate_report}.py
    │   └── references/{schema.json,severity-model.md,specialist-contract.md}
    ├── crawl-render-audit/
    │   ├── SKILL.md
    │   └── scripts/{check,robots,fetch,render,sitemap}.py
    ├── freshness-corroboration-audit/
    │   ├── SKILL.md
    │   └── scripts/{check,fetch}.py
    └── engagement-audit/
        ├── SKILL.md
        └── scripts/{check,fetch}.py
```

`fetch.py` is duplicated into each specialist so every skill folder is
self-contained and independently valid against the agentskills.io spec, which is
a requirement of the marketplace rules.

## Adding a specialist

Add a folder with a `SKILL.md` and `scripts/check.py` that satisfies
`references/specialist-contract.md`, then list it in `marketplace.json`. No
orchestrator code changes.

## Safety

Read-only, GET only, TLS verified, bounded bytes, redirects and time, polite
delays, `robots.txt` respected before any retrieval. Never authenticates, never
bypasses a CAPTCHA, WAF, paywall or consent wall. A consent wall is detected by
reading markup, never by dismissing it. Off-origin requests are limited to at
most three public `sameAs` URLs the site itself declares.
