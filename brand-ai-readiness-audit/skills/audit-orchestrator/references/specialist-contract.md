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
| `--user-agent` | marketplace bot string | UA used for policy evaluation and retrieval |
| `--timeout` | `8.0` | Per-request timeout, seconds |
| `--budget-ms` | `60000` | Wall-clock budget for this specialist. Must return before it elapses, degrading to limitations if needed |
| `--sample-file` | none | Path to the shared page sample JSON. When present, the specialist audits these URLs and no others |
| `--no-render` | off | Forbid headless rendering even if available |

**Shared sample rule.** Every specialist audits the same page set. If a specialist
selects its own pages, findings across skills stop being comparable and the audit stops
being reproducible. `sitemap.py` produces the sample; the orchestrator passes it to
everyone.

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

Scripts inside a specialist import each other as siblings. Each exposes one function.

```python
# robots.py
check_robots(url, user_agent=..., timeout=..., ai_agents=None) -> dict
normalize_url(raw_url) -> str

# fetch.py
fetch_page(url, user_agent=..., timeout=..., max_bytes=...) -> dict
#   -> outcome: "retrieved" | "unavailable" | "error"
#      http_status, final_url, redirect_chain, content_type,
#      body, bytes, truncated, error, duration_ms

# render.py
render_page(url, timeout=...) -> dict
#   -> available: bool, outcome, html, text, error, duration_ms

# sitemap.py
discover(url, declared_sitemaps, limit=...) -> dict
#   -> sitemaps, url_count, sample_urls, truncated, limitations
```

A missing sibling module is **not** an error. `check.py` records a limitation and
continues with the checks it can still perform.

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
