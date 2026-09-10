# Specialist Contract

Every non-entrypoint skill in this marketplace exposes exactly one executable entry:

```
python3 skills/<skill-id>/scripts/check.py <url> [options]
```

This is a marketplace convention, not part of the agentskills.io specification. It exists
so `dispatch.py` can invoke any specialist without knowing anything about it beyond its
path in `marketplace.json`. Adding a specialist requires no orchestrator change.

---

## 1. Invocation

| Option | Default | Meaning |
|---|---|---|
| `<url>` | required | Target URL or bare domain |
| `--evidence-file` | none | Path to the shared `evidence.json` produced once by `audit-orchestrator/scripts/evidence.py`. The canonical specialist input. A specialist analyzes this file and does not independently fetch pages. |
| `--user-agent` | marketplace bot string | Accepted for compatibility; collection (and therefore the effective UA) is owned by `evidence.py`, not by the specialist |
| `--timeout` | `8.0` | Accepted for compatibility with specialists that still perform their own bounded verification requests (e.g. `sameAs` checks belong to evidence collection, not here) |
| `--budget-ms` | `60000` | Wall-clock budget for this specialist. Must return before it elapses, degrading to limitations if needed |
| `--sample-file` | none | **Deprecated.** The pre-evidence interface: a bare URL list with no page content. Retained only so a specialist that has not yet migrated does not fail argument parsing; new specialists should not read it |
| `--no-render` | off | Accepted for compatibility. Whether rendering happens at all is decided once by `evidence.py`; a specialist reads `page.render` from the evidence file rather than requesting rendering itself |

**Shared evidence rule.** Every specialist analyzes the same `evidence.json`. If a
specialist independently re-fetches a sampled page, findings across skills stop being
comparable, network work is duplicated, and the audit stops being reproducible.
`evidence.py` collects once; the orchestrator passes the same evidence file path to
every specialist. See `evidence.json`'s schema, documented in `evidence.py`'s module
docstring and reflected in each page object's fields (`status`, `robots_allowed`,
`jsonld`, `open_graph`, `headings`, `links`, `date_signals`, `identity_signals`,
`same_as`, `engagement_signals`, `page_structure`, `render`, `limitations`) plus
`site_level` (`robots`, `sitemap`, `corroboration`) and top-level `limitations`.

A specialist that receives no `--evidence-file`, or an evidence file with an empty
`pages` array, must record a limitation and must not manufacture a finding from the
absence of evidence.

---

## 2. Output

`stdout` carries **exactly one JSON object and nothing else**. Diagnostics go to `stderr`.

```json
{
  "skill": "crawl-render-audit",
  "status": "success",
  "observations": [],
  "findings": [],
  "limitations": []
}
```

`status` is one of `success`, `partial_failure`, `failure`.

- `success` — every planned check ran.
- `partial_failure` — some checks ran, others could not. `limitations` explains which.
- `failure` — nothing usable was produced. `limitations` explains why.

**No findings is a valid, healthy result.** An empty `findings` array with
`status: success` means the site passed this specialist's checks.

---

## 3. Exit codes

| Code | Meaning |
|---|---|
| `0` | A well-formed result was printed. Applies to `success` and `partial_failure` |
| `1` | Specialist could produce no usable result. Still prints a `failure` object |
| `2` | Contract violation: bad arguments, unusable target |

A defect discovered on the audited website **never** produces a non-zero exit code.
Finding problems is the job succeeding, not failing.

---

## 4. Finding shape

Specialists emit a finding with local fields only. They never assign `severity`, a global
`id`, or a report position. Those belong to the orchestrator.

```json
{
  "local_id": "CR-002",
  "title": "AI assistant crawlers are disallowed by robots.txt",
  "category": "crawl_policy",
  "stage": "reachable",
  "confidence": "high",
  "scope": "site_wide",
  "affected_urls": ["https://example.com/"],
  "evidence": [
    "robots.txt at https://example.com/robots.txt returned HTTP 200.",
    "Group 'User-agent: GPTBot' line 14: Disallow: / matches the target path /."
  ],
  "severity_inputs": {
    "stage_block": 4,
    "fact_criticality": 3,
    "blast_radius": 3
  },
  "suggested_action": {
    "summary": "...",
    "rationale": "...",
    "effort": "low",
    "verify_by": "..."
  }
}
```

`severity_inputs` is mandatory. See `references/severity-model.md` for the value tables.
The orchestrator multiplies them and writes the band; a specialist that guesses a
severity label is a contract violation.

---

## 5. Limitation shape

```json
{
  "check": "render_diff",
  "reason": "No headless renderer available in this environment.",
  "affected_checks": ["client_side_content_dependence"]
}
```

A limitation is what keeps the report honest. Every check the specialist planned but did
not complete must appear here. Silence is not a pass.

---

## 6. Determinism rules

- No timestamps in specialist output. The orchestrator stamps `audited_at` once.
- No random values, no UUIDs, no PIDs, no wall-clock in any identifier.
- Sort every array before emitting: findings by `local_id`, evidence in the fixed order
  produced by the procedure, URL lists lexicographically.
- Durations may be reported in `observations`, never inside evidence strings used for
  deduplication.

---

## 7. Sibling module interfaces

`robots.py`, `fetch.py`, `render.py` and `sitemap.py` now live under
`audit-orchestrator/scripts/` and are siblings of `evidence.py` only. No specialist
imports them directly any more; a specialist's `check.py` reads their output
secondhand, through `evidence.json`. This is what stops network collection logic (and
network calls) from being duplicated across skills.

```python
# robots.py
check_robots(url, user_agent=..., timeout=..., ai_agents=None) -> dict
normalize_url(raw_url) -> str
fetch_robots_txt(robots_url, user_agent=..., timeout=...) -> FetchResult
parse_robots_txt(text) -> ParsedRobots
select_group(groups, user_agent) -> Optional[Group]
evaluate_path(group, path) -> tuple[bool, Optional[Rule]]

# fetch.py
fetch_page(url, user_agent=..., timeout=..., max_bytes=...) -> dict
#   -> outcome: "retrieved" | "unavailable" | "error"
#      http_status, final_url, redirect_chain, content_type,
#      body, bytes, truncated, error, duration_ms

# render.py
render_page(url, timeout=...) -> dict
#   -> available: bool, outcome, html, text, error, duration_ms

# sitemap.py
discover(url, declared_sitemaps, limit=..., timeout=..., user_agent=...) -> dict
#   -> sitemaps, url_count, sample, truncated, sample_strategy, limitations
```

`evidence.py` calls `robots.fetch_robots_txt()` and `robots.parse_robots_txt()` exactly
once per audit, then reuses the parsed rules to evaluate every sampled URL's
`robots_allowed` via `select_group()`/`evaluate_path()`, and passes the resulting
declared sitemaps into `sitemap.discover()` directly. This is what keeps robots.txt to a
single fetch per audit regardless of sample size.

A missing sibling module is **not** an error. `evidence.py` records a collection
limitation and continues with what it can still collect; a specialist reading a
resulting gap in `evidence.json` records its own limitation rather than treating the
gap as either a defect or a pass.

---

## 8. Safety invariants

Binding on every specialist, without exception:

- Read-only. No POST, PUT, PATCH, DELETE, no form submission, no state mutation.
- Never authenticate. Never send cookies obtained from a login.
- Never bypass `robots.txt`, a CAPTCHA, a WAF, a paywall or any access control.
- Bounded requests, bounded bytes, bounded redirects, polite delay between requests.
- Verify TLS. A certificate failure is reported, never disabled to force retrieval.
- Nothing outside the target origin is fetched except where a check explicitly requires
  third-party corroboration, and then only public pages.
