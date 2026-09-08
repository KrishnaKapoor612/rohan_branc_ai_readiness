# Brand AI Readiness Audit — UML Diagrams

Architecture of the **Brand AI Readiness Audit** marketplace (Adobe University
Hackathon 2026, Round 3).

A read-only agent skill marketplace that audits a public website for AI
discoverability and on-site engagement, and emits one deterministic,
evidence-backed report.

Four skills, one entrypoint, each owning a contiguous band of the discovery
funnel.

---

## 0. The model everything follows

```mermaid
flowchart LR
    E[exists] --> R[reachable]
    R --> RD[renderable]
    RD --> X[extractable]
    X --> U[unambiguous]
    U --> C[corroborated]
    C --> CU[current]
    CU --> EN[engaging]
    EN --> A[actionable]

    subgraph CRA["crawl-render-audit"]
        R
        RD
        X
    end
    subgraph FCA["freshness-corroboration-audit"]
        U
        C
        CU
    end
    subgraph EA["engagement-audit"]
        EN
    end
```

A defect belongs to the **earliest** stage it breaks. A finding downstream of an
upstream blocker is gated, not reported twice.

---

## 1. Component Diagram

```mermaid
flowchart TB
    subgraph ROOT["Marketplace Root"]
        MANIFEST["marketplace.json"]

        subgraph ORCH["audit-orchestrator (entrypoint)"]
            OSPEC["SKILL.md"]
            DISPATCH["scripts/dispatch.py"]
            MERGE["scripts/merge.py"]
            VALIDATE["scripts/validate_report.py"]
            SCHEMA["references/schema.json"]
            SEV["references/severity-model.md"]
            CONTRACT["references/specialist-contract.md"]
        end

        subgraph CRA["crawl-render-audit"]
            CCHECK["scripts/check.py"]
            ROBOTS["scripts/robots.py"]
            FETCH1["scripts/fetch.py"]
            RENDER["scripts/render.py (optional)"]
            SITEMAP["scripts/sitemap.py"]
        end

        subgraph FCA["freshness-corroboration-audit"]
            FCHECK["scripts/check.py"]
            FETCH2["scripts/fetch.py"]
        end

        subgraph EA["engagement-audit"]
            ECHECK["scripts/check.py"]
            FETCH3["scripts/fetch.py"]
        end
    end

    subgraph TARGET["Target System"]
        SITE["Public website"]
        RTXT["robots.txt"]
        SMAP["Sitemap XML"]
        SAMEAS["Declared sameAs profiles"]
    end

    OSPEC --> DISPATCH
    DISPATCH -->|reads skills + entrypoint| MANIFEST
    DISPATCH -->|--emit-sample| SITEMAP
    SITEMAP -->|sample.json| DISPATCH
    DISPATCH -->|check.py url --sample-file| CCHECK
    DISPATCH -->|check.py url --sample-file| FCHECK
    DISPATCH -->|check.py url --sample-file| ECHECK

    CCHECK --> ROBOTS
    CCHECK --> FETCH1
    CCHECK --> RENDER
    FCHECK --> FETCH2
    ECHECK --> FETCH3

    ROBOTS --> RTXT
    SITEMAP --> SMAP
    FETCH1 --> SITE
    FETCH2 --> SITE
    FETCH2 -.->|max 3, existence only| SAMEAS
    FETCH3 --> SITE
    RENDER -.->|only if a browser exists| SITE

    CCHECK -->|specialist JSON| DISPATCH
    FCHECK -->|specialist JSON| DISPATCH
    ECHECK -->|specialist JSON| DISPATCH

    DISPATCH -->|aggregate JSON| MERGE
    MERGE -.->|applies| SEV
    MERGE --> VALIDATE
    VALIDATE -.->|checks against| SCHEMA
    VALIDATE --> REPORT["Final audit report (JSON)"]

    CONTRACT -.->|governs| CCHECK
    CONTRACT -.->|governs| FCHECK
    CONTRACT -.->|governs| ECHECK
```

---

## 2. Class Diagram

```mermaid
classDiagram
    class MarketplaceManifest {
        +str name
        +str version
        +str entrypoint
        +List~SkillDeclaration~ skills
    }
    class SkillDeclaration {
        +str id
        +str path
        +bool entrypoint
        +List~str~ provides
    }

    class PageSample {
        +str origin
        +str strategy
        +int pages_inspected
        +List~str~ urls
        +bool truncated
    }

    class SpecialistResult {
        +str skill
        +str status
        +List~str~ observations
        +List~SpecialistFinding~ findings
        +List~Limitation~ limitations
        +List~Recommendation~ proactive_recommendations
        +int duration_ms
    }

    class SpecialistFinding {
        +str local_id
        +str title
        +str category
        +str stage
        +str confidence
        +str scope
        +List~str~ affected_urls
        +List~str~ evidence
        +SeverityInputs severity_inputs
        +SuggestedAction suggested_action
    }

    class SeverityInputs {
        +int stage_block
        +int fact_criticality
        +int blast_radius
        +int score
        +compute() Severity
    }

    class SuggestedAction {
        +str summary
        +str priority
        +str rationale
        +str effort
        +str verify_by
    }

    class Limitation {
        +str skill
        +str check
        +str reason
        +List~str~ affected_checks
    }

    class Recommendation {
        +str title
        +str summary
        +str rationale
        +str priority
        +str category
    }

    class AuditReport {
        +str schema_version
        +str site
        +str audited_at
        +AuditMetadata audit
        +AuditSummary summary
        +List~FinalFinding~ findings
        +List~SuppressedFinding~ suppressed_findings
        +List~Limitation~ limitations
        +List~Recommendation~ proactive_recommendations
    }

    class FinalFinding {
        +str id
        +Severity severity
        +str blocked_by
        +str source_skill
    }

    class SuppressedFinding {
        +str title
        +str stage
        +str blocked_by
        +str reason
    }

    class Severity {
        <<enumeration>>
        CRITICAL
        HIGH
        MEDIUM
        LOW
    }

    MarketplaceManifest "1" *-- "1..*" SkillDeclaration
    SpecialistResult "1" *-- "0..*" SpecialistFinding
    SpecialistResult "1" *-- "0..*" Limitation
    SpecialistResult "1" *-- "0..*" Recommendation
    SpecialistFinding "1" *-- "1" SeverityInputs
    SpecialistFinding "1" *-- "1" SuggestedAction
    SpecialistFinding <|-- FinalFinding : scored and numbered by merge.py
    AuditReport "1" *-- "0..*" FinalFinding
    AuditReport "1" *-- "0..*" SuppressedFinding
    AuditReport "1" *-- "0..*" Limitation
    FinalFinding "1" o-- "1" Severity
```

Note the direction: a specialist produces `SeverityInputs`, never a `Severity`.
The enum is only ever populated by `merge.py`.

---

## 3. Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    actor Agent as Agent / User
    participant O as audit-orchestrator
    participant D as dispatch.py
    participant M as marketplace.json
    participant SM as sitemap.py (sampler)
    participant S as specialist check.py
    participant MG as merge.py
    participant V as validate_report.py

    Agent->>O: audit request (URL)
    O->>O: validate and normalise URL
    O->>D: dispatch.py <url>

    D->>M: read manifest
    M-->>D: skills, entrypoint flag, provides
    D->>D: exactly one entrypoint? else fail closed

    D->>SM: sitemap.py <url> --emit-sample
    SM->>SM: robots sitemaps, then /sitemap.xml, bounded
    SM-->>D: shared sample (8-12 URLs, round-robin by path role)
    D->>D: write sample.json to work dir

    loop each specialist, in manifest order
        D->>S: check.py <url> --sample-file sample.json
        S->>S: robots, retrieve, analyse sampled pages
        alt well-formed JSON on stdout
            S-->>D: observations, findings, limitations, recommendations
        else crash / timeout / non-JSON / missing field
            D->>D: synthesise failure result + limitation
        end
    end

    D-->>MG: aggregate (results, sample, limitations, recommendations)

    MG->>MG: 1. deduplicate by category + stage + surface
    MG->>MG: 2. gate (hard block gates all; policy block gates discoverability only)
    MG->>MG: 3. score = stage_block x fact_criticality x blast_radius
    MG->>MG: 4. canonical sort, then number F001..
    MG-->>V: candidate report

    V->>V: schema conformance (jsonschema if present, else built-in)
    V->>V: invariants: ID contiguity, counts, ordering, severity match, references
    alt valid
        V-->>O: validated report
        O-->>Agent: one JSON object
    else invalid
        V-->>O: error list
        O->>O: deterministic correction, else record as limitation
        O-->>Agent: report with limitations
    end
```

---

## 4. Activity Diagram

```mermaid
stateDiagram-v2
    [*] --> Validate: URL supplied
    Validate --> ReadManifest: normalised
    Validate --> Reject: malformed
    Reject --> [*]

    ReadManifest --> ManifestInvalid: entrypoint count != 1
    ManifestInvalid --> [*]
    ReadManifest --> BuildSample: one entrypoint found

    state BuildSample {
        [*] --> FindSampler: provides page_sample
        FindSampler --> FallbackSampler: none declared
        FallbackSampler --> RunSampler: first skill shipping sitemap.py
        FindSampler --> RunSampler
        RunSampler --> SampleReady
        RunSampler --> SampleFallback: timeout or failure
        SampleFallback --> SampleReady: target URL only + limitation
    }

    SampleReady --> RunSpecialists

    state RunSpecialists {
        [*] --> Invoke
        Invoke --> Parsed: valid JSON, contract fields present
        Invoke --> Isolated: crash, timeout, non-JSON, missing field
        Isolated --> Parsed: failure result + limitation
        Parsed --> Next
        Next --> Invoke: more specialists
        Next --> [*]: done
    }

    RunSpecialists --> Deduplicate

    state Compose {
        Deduplicate --> Score: one defect per category+stage+surface
        Score --> Gate: severity from severity_inputs
        state Gate {
            [*] --> AnyBlocker
            AnyBlocker --> NoGating: no reachable-stage finding
            AnyBlocker --> HardBlock: category = access
            AnyBlocker --> PolicyBlock: category = crawl_policy
            HardBlock --> SuppressAll: every later stage
            PolicyBlock --> SuppressDiscovery: discoverability chain only
            PolicyBlock --> KeepEngagement: engagement still observable
        }
        Gate --> Sort: canonical key
        Sort --> Number: F001, F002, ...
    }

    Deduplicate --> Compose
    Number --> Validate2: schema + invariants

    Validate2 --> Emit: valid
    Validate2 --> Correct: deterministic fix available
    Correct --> Emit
    Validate2 --> EmitWithLimitation: not deterministically fixable
    Emit --> [*]
    EmitWithLimitation --> [*]
```

---

## 5. Package Diagram

```mermaid
flowchart TB
    MP["marketplace.json"]
    ORCH["audit-orchestrator<br/>dispatch, merge, validate_report<br/>schema, severity-model, specialist-contract"]
    CRA["crawl-render-audit<br/>check, robots, fetch, render, sitemap"]
    FCA["freshness-corroboration-audit<br/>check, fetch"]
    EA["engagement-audit<br/>check, fetch"]

    MP -.->|declares entrypoint| ORCH
    MP -.->|declares specialist| CRA
    MP -.->|declares specialist| FCA
    MP -.->|declares specialist| EA
    ORCH -->|check.py interface| CRA
    ORCH -->|check.py interface| FCA
    ORCH -->|check.py interface| EA
    ORCH -.->|reads| MP
```

Dependency direction runs one way only. No specialist imports the orchestrator,
and no specialist imports another specialist. `fetch.py` is duplicated rather
than shared so each skill folder stays independently valid.

---

## 6. Architectural highlights

| Pattern | Implementation | Why it matters |
|---|---|---|
| **Funnel-banded decomposition** | each skill owns contiguous stages of `reachable -> actionable` | the split is a separation of concerns, not padding, and it gives fix order for free |
| **Marketplace / plugin** | `marketplace.json` declares skills, entrypoint and `provides` | adding a specialist needs no orchestrator change |
| **Shared page sample** | `sitemap.py --emit-sample`, passed to every specialist | findings are comparable across skills and reproducible between runs; `blast_radius` follows a real ratio |
| **Observe / score split** | specialists emit `severity_inputs`; only `merge.py` writes `severity` | severity is a property of the marketplace, not of whichever skill noticed |
| **Two-tier blocker reach** | hard access failures gate everything; policy blocks gate discoverability only | one root cause is not reported four times, and the engagement half is never silently dropped |
| **Fault isolation** | crash, timeout, non-JSON and contract breaches all become `limitations` | one broken specialist cannot end the audit or fake a clean result |
| **Capability honesty** | `render.py` reports `available: false`; missing capabilities downgrade `confidence` | absence of a tool never manufactures a finding |
| **Contract-driven validation** | `validate_report.py` enforces schema **and** internal invariants | a schema-valid but self-contradictory report is worse than none |
| **Determinism** | fixed sort key, no timestamps or randomness in identifiers | identical inputs produce a byte-identical report |
