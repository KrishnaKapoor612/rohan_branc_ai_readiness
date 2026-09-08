# Severity Model

Authoritative reference for how every finding in this marketplace is scored.

Specialists **observe**. They emit `severity_inputs`, never a severity label.
The orchestrator **scores**. It applies the arithmetic below and writes `severity`.

This split exists so that severity is a property of the marketplace, not of whichever
skill happened to notice the problem. It is also what makes the audit deterministic:
identical inputs produce identical severities on every run, with no model judgement in
the loop.

---

## 1. The formula

```
score = stage_block x fact_criticality x blast_radius
```

Three small integers, multiplied. Range 1 to 36.

| Band | Score | Meaning |
|---|---|---|
| `critical` | 27 to 36 | The brand is structurally absent or wrong at scale. Nothing downstream can compensate. |
| `high` | 14 to 26 | A commercially load-bearing fact is unavailable or untrusted across a whole template. |
| `medium` | 6 to 13 | A real defect with bounded reach, or a secondary fact affected site-wide. |
| `low` | 1 to 5 | Worth fixing, will not change whether the brand gets named. |

---

## 2. `stage_block` (1 to 4)

Where in the discovery funnel the defect breaks the chain. Earlier failures score higher
because everything after them is unreachable regardless of quality.

```
exists -> reachable -> renderable -> extractable -> unambiguous
       -> corroborated -> current -> engaging -> actionable
```

| Value | Stage | Typical defects |
|---|---|---|
| 4 | `reachable` | robots.txt disallow, HTTP 403/5xx, TLS failure, redirect loop, bot challenge, no crawl path to the page |
| 3 | `renderable` | Content exists only after client-side execution; server response is a shell |
| 2 | `extractable` | Fact present but not machine-readable: locked in an image, unlabelled table cell, no structured data, no clear sentence |
| 2 | `unambiguous` | Entity confusion: no canonical name string, no `sameAs` graph, brand name collides with another entity |
| 2 | `corroborated` / `current` | Fact stated only on the brand's own site, or contradicted by third-party sources, or undated |
| 2 | `engaging` | Visitor lands and cannot orient: no deep-link target, no answer-first content, blocking interstitial |
| 1 | `actionable` | Brand is found and correct but an agent cannot verify stock, price or availability |

**Rule:** a defect is assigned the *earliest* stage it breaks, never the most dramatic one.
A price that is missing because the whole `/products/` path is disallowed is a
`reachable` defect, not an `extractable` one.

---

## 3. `fact_criticality` (1 to 3)

How load-bearing the affected claim is for the buying decision. Judged against the
site's archetype, not in the abstract.

| Value | Class | Examples |
|---|---|---|
| 3 | Identity and commercial | Legal or brand name, current tagline and logo, product model name, price, availability, store locations, opening hours, contact |
| 2 | Primary content | What the product does, category positioning, materials, specifications, core service description |
| 1 | Supporting | Blog posts, press archive, careers, secondary editorial |

---

## 4. `blast_radius` (1 to 3)

| Value | Scope | Definition |
|---|---|---|
| 3 | `site_wide` or `template` | Affects the whole origin, or every page of the site's primary commercial template (all product pages on a store, all listing pages on a marketplace) |
| 2 | `section` | Affects one section or a secondary template |
| 1 | `single_page` | One page only |

**Rule:** never infer `site_wide` from one page. Scope must be supported by the number of
sampled pages actually inspected, and the evidence string must state the ratio, for
example `0/12 product pages`.

---

## 5. Confidence caps

`confidence` is reported separately and never inflates severity. It only caps it.

| Confidence | Meaning | Cap |
|---|---|---|
| `high` | Directly observed by an executed check | No cap |
| `medium` | Inferred from a proxy signal because a capability was unavailable (for example, client-side dependence inferred from a hydration payload because no renderer was present) | Maximum `high` |
| `low` | Not permitted on a finding. Record as a `limitation` or an `observation` instead | Not a finding |

This encodes the marketplace's central evidence rule: a check that could not run is
neither a defect nor a pass.

---

## 6. Worked examples

| Defect | stage | crit | radius | Score | Severity |
|---|---|---|---|---|---|
| `robots.txt` disallows all AI crawler tokens site-wide | 4 | 3 | 3 | 36 | critical |
| Origin returns 403 to non-browser user agents while serving browsers | 4 | 3 | 3 | 36 | critical |
| No structured data on 0/12 sampled product pages | 2 | 3 | 3 | 18 | high |
| Price present in rendered DOM, absent from server HTML, all product pages | 3 | 3 | 2 | 18 | high |
| Current tagline contradicted by third-party sources | 2 | 3 | 3 | 18 | high |
| `/blog` disallowed in robots.txt | 4 | 1 | 3 | 12 | medium |
| No stable heading anchors anywhere, so assistants can only link the homepage | 2 | 2 | 3 | 12 | medium |
| No product feed or availability endpoint for agent verification | 1 | 3 | 3 | 9 | medium |
| Single product page returns 404 from the sitemap | 4 | 3 | 1 | 12 | medium |
| Key spec rendered as text inside a hero image on the about page | 2 | 1 | 1 | 2 | low |

---

## 7. Suggested-action priority

`severity` answers "how bad". `priority` answers "what first". They differ because
fixing an upstream blocker unlocks several downstream checks at once.

```
base     = high   if severity in {critical, high}
           medium if severity == medium
           low    if severity == low

priority = promote one level if this finding appears in any other finding's
           `blocked_by`, or if any entry in `suppressed_findings` names it
```

`low -> medium -> high`, capped at `high`.

---

## 8. Canonical ordering

Findings are sorted, then numbered `F001`, `F002`, and so on. Ordering is fully
specified so that IDs are stable across runs:

1. `severity` descending: `critical`, `high`, `medium`, `low`
2. `stage_block` descending (earliest funnel stage first)
3. `category` ascending, lexicographic
4. first entry of `affected_urls` ascending, lexicographic
5. `title` ascending, lexicographic

No timestamps, no hash ordering, no reliance on dictionary or set iteration order at any
point in the pipeline.

---

## 9. Refusals

The model deliberately produces **no** severity in these cases. They are limitations, not
findings:

- A check that timed out, was blocked by a challenge page, or hit a disallow rule.
- A single environment-specific failure generalised to the whole site.
- The absence of an optional artifact with no demonstrated impact, such as a missing
  sitemap on a small site whose pages are all reachable through crawlable links.
- Any condition whose only supporting evidence is the absence of evidence.
