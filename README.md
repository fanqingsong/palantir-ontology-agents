# Palantir Ontology Agents

> **Alex Karp**: *"The demand for AIP, in virtually every context, is extraordinary."*
>
> This multi-agent system shows what agentic workflows on the ontology look like in practice.

A working multi-agent system built with LangGraph that demonstrates how specialist agents coordinate over an ontology-structured data layer to produce actionable intelligence -- mirroring Palantir AIP's agentic workflow architecture.

**Demo scenario**: 4 agents analyze a Taiwan Strait supply chain disruption, traverse a 50-entity ontology graph, assess threats, and deliver an executive briefing in under 2 minutes.

## What that sentence means

Palantir AIP is not "several chatbots." Agents share one typed object graph and hand off structured state. This repo is a runnable sketch of that pattern.

| Clause | Meaning here |
|--------|----------------|
| **A working multi-agent system** | An executable path, not a slide: shared state, real nodes, a briefing at the end. Four specialists, 50+ entities, 100+ edges. |
| **built with LangGraph** | A `StateGraph` decides order: Coordinator, then OSINT, then Graph Analyst (same store), then Threat Assessor, then Briefing Drafter. |
| **coordinate over an ontology-structured data layer** | Agents query typed entities and labeled edges (SUPPLIES, DEPENDS_ON, THREATENS), not a pile of unaligned web pages. |
| **produce actionable intelligence** | Exposure scores, threat assessment, and an executive briefing — not another news recap. |

Two layers, one pipeline:

```text
Orchestration (LangGraph)     Data (Ontology Store)
  who runs, in what order       typed objects + semantic edges
  src/graph/workflow.py         src/ontology/
```

```mermaid
flowchart LR
  Apple -->|DEPENDS_ON| TSMC
  ASML -->|SUPPLIES| TSMC
  TSMC -->|OPERATES_IN| KaohsiungPort
  PLA_Navy -->|THREATENS| KaohsiungPort
  Blockade -->|THREATENS| KaohsiungPort
```

Read the graph: Apple depends on TSMC; ASML supplies TSMC; TSMC ships through Kaohsiung; navy and a strait blockade both threaten that port. Exposure walks upstream along `DEPENDS_ON`.

| Question | LLM + web pages | Agents on an ontology |
|----------|-----------------|------------------------|
| Is Apple hit by a port blockade? | Model guesses from memory or retrieved paragraphs | Walk `DEPENDS_ON` / `OPERATES_IN`; the score is reproducible |
| How do agents name the same company? | Separate summaries; "TSMC" vs "台积电" may never join | Shared entity IDs and edges |
| What is the output? | Fluent text that is hard to audit | Graph evidence + threat score + structured briefing |

## Demo

![Demo](docs/demo.gif)

[Watch full quality video](https://github.com/hashwnath/palantir-ontology-agents/releases/download/v1.0/demo.mp4)

## Architecture

```mermaid
graph TD
    User([User Query]) --> C[Coordinator Agent]
    C --> O[OSINT Collector]
    O --> WS[Web Search - Tavily]
    O --> ONT[(Ontology Store)]
    O --> G[Graph Analyst]
    G --> ONT
    G --> T[Threat Assessor]
    T --> TDB[(Threat Database)]
    T --> B[Briefing Drafter]
    B --> DG[Document Generation]
    B --> OUT([Executive Briefing])

    style C fill:#90EE90,stroke:#333
    style O fill:#90EE90,stroke:#333
    style G fill:#90EE90,stroke:#333
    style WS fill:#90EE90,stroke:#333
    style ONT fill:#90EE90,stroke:#333
    style T fill:#FFD700,stroke:#333
    style B fill:#FFD700,stroke:#333
    style TDB fill:#FFD700,stroke:#333
    style DG fill:#FFD700,stroke:#333
```

**Green** = Live (working code) | **Yellow** = Scaffolded (mock data, clean integration points)

Orchestration is the LangGraph path above. The **Ontology Store** is one process-shared graph (`get_shared_store()`): OSINT writes first, Graph Analyst reads the same instance after an optional outbox drain. LangGraph state keeps light stats (`ontology_store_data`), not a full graph dump.

In **dual** mode (Docker Compose default), Postgres is the system of record and Neo4j is a **graph projection** fed by an outbox pipeline (CDC → Kafka → Prefect). Writes do not block on Neo4j unless you opt into synchronous flush.

The two live specialists use that graph differently: OSINT **aligns** open-source text onto canonical IDs (and writes events plus `RELATED_TO` edges); Graph Analyst **reads** the same typed edges to compute chains, hubs, and exposure.

## How OSINT and Graph Analyst use the ontology

```text
Query
  ├─ OSINT Collector     web text ──match──► canonical IDs ──write──► EVENT nodes
  │                      co-occurrence ──► RELATED_TO ──write──► store
  └─ Graph Analyst       keywords ──► focus nodes ──traverse / path / degree / exposure──► findings
```

```mermaid
flowchart TB
  subgraph OSINT["OSINT Collector — align + light write"]
    direction TB
    W[Web search] --> X[Keyword extract]
    X --> A["store.get_entity(id) → canonical name"]
    X --> R[Co-occurrence RELATED_TO]
    F[Key findings] --> D["store.search() de-dupe"]
    D --> E["add_entity(EVENT)"]
  end

  subgraph GA["Graph Analyst — read-only graph ops"]
    direction TB
    K[Query keywords] --> FO["get_entity — focus nodes"]
    FO --> C["DEPENDS_ON / SUPPLIES chains"]
    FO --> P["BFS shortest path vs threats"]
    FO --> H["degree = |neighbors|"]
    FO --> S["exposure from hop distance"]
  end

  ONT[(Ontology Store<br/>typed entities + labeled edges)]
  A --> ONT
  E --> ONT
  R --> ONT
  ONT --> FO
  ONT --> C
  ONT --> P
  ONT --> H
  ONT --> S

  style OSINT fill:#e8f5e9,stroke:#333
  style GA fill:#e3f2fd,stroke:#333
  style ONT fill:#90EE90,stroke:#333
```

| Agent | Ontology role | Writes the store? |
|-------|---------------|-------------------|
| **OSINT Collector** | Dictionary of known IDs (`tsmc`, `taiwan_strait`, …) plus a place to append OSINT events and co-occurrence edges | Yes — new `EVENT` nodes and `RELATED_TO` edges |
| **Graph Analyst** | The analysis object: traversal, supply-chain chains, hubs, exposure | No |

### OSINT Collector: dictionary, then a few events

Pipeline in `src/agents/osint_agent.py`: search → extract entities → key findings → infer relationships → optional store update.

`ENTITY_PATTERNS` maps phrases in titles/snippets onto **fixed ontology IDs**. If the store already has that ID, the agent uses the official `name` so "Taiwan Semiconductor" and "TSMC" collapse to one object:

```mermaid
flowchart LR
  T1["'TSMC'"] --> ID((tsmc))
  T2["'Taiwan Semiconductor'"] --> ID
  ID --> N["store.get_entity('tsmc').name → TSMC"]
```

Same-article co-mentions become `RELATED_TO` edges with confidence `score * 0.8`. They are recorded in `OSINTResult.new_relationships` **and** written into the shared store as `osint_{source}_{target}` when both endpoints already exist.

Each key finding is searched with `store.search(finding[:30])`. On a miss, a new `EntityType.EVENT` is added (`source="osint_agent"`, `confidence=0.7`). Existing orgs, ports, and threats are not mutated.

```mermaid
sequenceDiagram
  participant Web as Search results
  participant Pat as ENTITY_PATTERNS
  participant Store as OntologyStore
  participant Out as OSINTResult

  Web->>Pat: title + snippet
  Pat->>Store: get_entity(canonical id)
  Store-->>Out: extracted_entities (aligned names)
  Pat->>Store: add_relationship RELATED_TO
  Store-->>Out: new_relationships
  Web->>Store: search(finding) then maybe add_entity(EVENT)
```

### Graph Analyst: the graph *is* the analysis

Without a store, `run()` returns immediately (`"No ontology store available"`). With one, `src/agents/graph_agent.py` plus `src/tools/ontology_tools.py` do seven read-only steps:

```mermaid
flowchart LR
  Q[Query] --> F[Focus IDs]
  F --> DC[Dependency chains]
  F --> EX[Exposure report]
  F --> CP[Critical paths]
  F --> HB[Hubs]
  F --> TR[3-hop traversal stats]
  DC --> KF[Key findings]
  EX --> KF
  CP --> KF
  HB --> KF
  TR --> KF
```

| Step | Store / tool API | What it means on the graph |
|------|------------------|----------------------------|
| Focus entities | `get_entity(id)` | Keywords map to `tsmc`, `taiwan_strait`, `uspacflt`, …; default to those three if nothing matches |
| Dependency chains | `get_dependency_chains` | Follow **outgoing** `DEPENDS_ON` / `SUPPLIES` / `SUPPLIES_TO`, max depth 5 |
| Exposure | `query_by_type(ORGANIZATION)` + `calculate_exposure_score` | BFS to nearest `THREAT`; score `max(0, 1.0 − (hops − 1) × 0.2)` |
| Critical paths | `query_by_type(THREAT)` + `find_path` (max 5 hops) | Shortest paths among focus ∪ threats; keep 20 shortest (BFS, or Cypher on Neo4j) |
| Hubs | `all_entities()` + `get_neighbors` | Degree centrality, top 10 |
| Stats | `traverse(..., hops=3)` | Reachable size from each focus node |
| Findings | (derived) | Most-connected node, high exposure, longest chain, ≤2-hop threat paths |

Exposure is hop distance, not an LLM guess:

```mermaid
flowchart LR
  Apple -->|1 hop| TSMC
  TSMC -->|2 hops| Port
  Port -->|3 hops| Blockade[[THREAT]]

  Apple -.->|score 0.6| Blockade
```

1 hop ≈ **1.0**, each extra hop minus **0.2**. Graph Analyst does not write edges; the `llm` constructor argument is unused.

### Same query, one shared graph

Coordinator, OSINT, and Graph Analyst call `get_shared_store()`. The workflow is sequential so OSINT commits (and dual-mode outbox is drained) before Graph Analyst traverses. New `EVENT` nodes and `RELATED_TO` edges from this run are visible to graph analysis.

```mermaid
flowchart TB
  C[Coordinator] --> S[(shared OntologyStore)]
  C --> O[OSINT]
  O --> S
  O --> G[Graph Analyst]
  G --> S
```

Downstream Threat Assessor / Briefing Drafter still consume **serialized results** (`osint_results`, `graph_results`) for the briefing. The live graph is the shared store, not a per-agent copy.

## Live vs Blueprint

| Component | Status | Notes |
|-----------|--------|-------|
| Coordinator Agent | **Live** | Routes queries, manages ontology state |
| OSINT Collector | **Live** | Web search via Tavily, entity extraction |
| Graph Analyst | **Live** | Ontology traversal, relationship analysis, exposure scoring |
| Ontology Store | **Live** | memory / postgres / neo4j / **dual** (PG + async outbox → Neo4j) |
| Outbox projection | **Live** | Debezium CDC, Kafka, Prefect flows, reconcile schedule (Compose) |
| Web Search Tool | **Live** | Tavily API with demo mode fallback |
| Threat Assessor | Blueprint | Mock threat data, clean integration points for classified feeds |
| Briefing Drafter | Blueprint | LLM generation with template, integration points for Palantir doc system |
| Threat Database | Blueprint | Sample data, documented API for real threat feeds |
| Document Generation | Blueprint | Template output, integration points for Palantir document pipeline |
| Foundry API Connector | Blueprint | Documented interface, mock responses |

## Quick Start

### Docker Compose（推荐）

```bash
git clone https://github.com/hashwnath/palantir-ontology-agents.git
cd palantir-ontology-agents

# Required for Run Analysis: OPENAI_API_KEY (OpenAI-compatible)
# Optional: OPENAI_BASE_URL / OPENAI_MODEL / TAVILY_API_KEY
cp .env.example .env

docker compose up --build
```

| Service | URL / port | Role |
|---------|------------|------|
| **app** | http://localhost:8501 | Streamlit demo (`ONTOLOGY_BACKEND=dual`, `OUTBOX_SYNC_FLUSH=false`) |
| **prefect-server** | http://localhost:4200 | Self-hosted Prefect UI + API |
| **neo4j** | http://localhost:7474 | Graph browser (`neo4j` / `ontology-dev`) |
| **postgres** | localhost:5432 | Ontology DB + outbox + Prefect metadata DB (`prefect`) |
| **kafka** | localhost:9092 | Single-node broker (Bitnami KRaft) |
| **kafka-connect** | localhost:8083 | Debezium Connect REST (`quay.io/debezium/connect:2.7`) |

Background services (no UI): **prefect-worker**, **outbox-bridge**, **debezium-init**. What each container does, and how they wire together, is in [Compose microservices](#compose-microservices).

Most Compose images use the Huawei SWR docker.io mirror (`swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/...`). Two tags are not available there:

| Service | Image | Why |
|---------|-------|-----|
| **neo4j** | `.../library/neo4j:5-community` | Floating `neo4j:5` is missing on that mirror. |
| **kafka-connect** | `quay.io/debezium/connect:2.7` | Debezium 2.7+ is published on Quay; the Huawei docker.io path 404s or requires login. |

App and worker images still use the Huawei `python:3.11-slim` base and Tsinghua PyPI.

```bash
# Stop (keep volumes)
docker compose down

# Reset DBs (required once when enabling CDC: wal_level=logical + prefect DB)
docker compose down -v
docker compose up --build
```

Set `ONTOLOGY_BACKEND=postgres` or `neo4j` in `.env` to use a single backend. Set `OUTBOX_SYNC_FLUSH=true` to project to Neo4j on every write inside the app process (no Kafka/Prefect needed for projection).

### Local

```bash
# Clone
git clone https://github.com/hashwnath/palantir-ontology-agents.git
cd palantir-ontology-agents

# Install
pip install -r requirements.txt

# OPENAI_API_KEY is required to run analysis
export TAVILY_API_KEY=your_tavily_key                    # optional: live OSINT web search
export OPENAI_API_KEY=your_openai_compatible_key         # required: LLM-powered analysis
export OPENAI_BASE_URL=https://api.openai.com/v1         # OpenAI-compatible base URL
export OPENAI_MODEL=gpt-4o-mini                          # model name on that endpoint

# Default: in-memory store. For Postgres / Neo4j / dual see .env.example
export ONTOLOGY_BACKEND=memory

# Run the Streamlit demo
streamlit run src/ui/app.py

# Run tests
pytest tests/ -v

# Run the orchestrator directly
python -c "
from src.orchestrator import Orchestrator
orch = Orchestrator()
result = orch.run('Track supply chain disruptions in Taiwan Strait, assess partner exposure, draft SECDEF briefing')
print(result['briefing']['briefing_text'])
"
```

## Compose microservices

`docker-compose.yml` starts **nine** services. They are not nine copies of the same app. Four planes share one ontology write:

```text
  Demo UI          app  ──writes──►  Postgres (source of truth)
                                      │
  Data stores      postgres + neo4j   │ CDC
                                      ▼
  Change capture   kafka-connect → kafka → outbox-bridge
                                      │
  Job runner       prefect-server ←───┘
                   prefect-worker ──MERGE──► Neo4j (graph projection)
```

```mermaid
flowchart TB
  subgraph demo["1. Demo"]
    APP["app<br/>Streamlit :8501"]
  end

  subgraph stores["2. Ontology stores"]
    PG[("postgres :5432<br/>DB ontology — entities / outbox<br/>DB prefect — Prefect metadata")]
    N4J[("neo4j :7687 / :7474<br/>typed graph projection")]
  end

  subgraph cdc["3. Change capture"]
    KC["kafka-connect :8083<br/>Debezium Connect"]
    DI["debezium-init<br/>one-shot: register connector"]
    K["kafka :9092<br/>topic ontology.public.outbox"]
  end

  subgraph jobs["4. Projection jobs"]
    PS["prefect-server :4200<br/>API + UI"]
    PW["prefect-worker<br/>claim row, MERGE Neo4j"]
    BR["outbox-bridge<br/>Kafka → Prefect flow run"]
  end

  APP -->|"upsert entity/rel + INSERT outbox"| PG
  APP -->|"read entities"| PG
  APP -->|"traverse after drain"| N4J

  PG -->|"logical WAL / pgoutput"| KC
  DI -.->|"POST /connectors"| KC
  KC -->|"CDC JSON"| K
  K -->|"consume group outbox-prefect-bridge"| BR
  BR -->|"create_flow_run<br/>project-outbox-row"| PS
  PS --> PW
  PW -->|"claim pending row"| PG
  PW -->|"MERGE / DELETE"| N4J
  PS -->|"metadata"| PG

  style APP fill:#90EE90,stroke:#333
  style PG fill:#cfe8ff,stroke:#333
  style N4J fill:#ffd9b3,stroke:#333
  style K fill:#e6d5ff,stroke:#333
  style KC fill:#e6d5ff,stroke:#333
  style DI fill:#f0f0f0,stroke:#333
  style PS fill:#ffe6a0,stroke:#333
  style PW fill:#ffe6a0,stroke:#333
  style BR fill:#ffe6a0,stroke:#333
```

**Postgres is the system of record. Neo4j is a projection.** The CDC + Prefect path exists so the Streamlit app does not block on graph writes (`OUTBOX_SYNC_FLUSH=false` by default). The agent write/read contract for that path is in [Dual mode: outbox and Neo4j projection](#dual-mode-outbox-and-neo4j-projection).

### What each service does

| Service | Image / process | Does | Does not |
|---------|-----------------|------|----------|
| **postgres** | `postgres:16-alpine` with `wal_level=logical` | Holds `entities`, `relationships`, transactional `outbox`; also hosts the `prefect` metadata database | Run agents or talk to Neo4j |
| **neo4j** | `neo4j:5-community` | Graph browser + Bolt API for typed labels and Cypher `shortestPath` | Accept app writes in dual mode (projection only) |
| **app** | `Dockerfile` → `streamlit run src/ui/app.py` | LangGraph demo: OSINT writes Postgres (+ outbox), Graph Analyst traverses Neo4j after `drain_outbox()` | Consume Kafka or start Prefect runs |
| **kafka** | Bitnami Kafka 3.7 (KRaft, single node) | Carries Debezium CDC for `public.outbox` | Know about ontology types |
| **kafka-connect** | `quay.io/debezium/connect:2.7` | Runs the Postgres connector; reads WAL, publishes to `ontology.public.outbox` | Register itself (see **debezium-init**) |
| **debezium-init** | `curl` one-shot | Waits for Connect, `POST`s `infra/debezium/postgres-outbox.json`, exits 0 on HTTP 201/409 | Stay running |
| **prefect-server** | `prefect server start :4200` | API + UI for deployments and flow runs | Execute projection Python |
| **prefect-worker** | `scripts/prefect-worker-entrypoint.sh` | Creates work pool, deploys `prefect.yaml`, runs `project-outbox-row` / `reconcile-pending-outbox` | Subscribe to Kafka |
| **outbox-bridge** | `python -m src.workers.kafka_bridge` | Consumes CDC; on INSERT or retry-to-`pending`, triggers `project-outbox-row/project-outbox-row` | Write Neo4j or claim outbox rows |

```mermaid
flowchart LR
  subgraph write["User hits Run Analysis"]
    U[Browser] --> APP[app]
    APP --> PG[(postgres)]
  end

  subgraph async["Meanwhile, projection"]
    PG --> KC[kafka-connect]
    KC --> K[kafka]
    K --> BR[outbox-bridge]
    BR --> PS[prefect-server]
    PS --> PW[prefect-worker]
    PW --> N4J[(neo4j)]
    PW --> PG
  end
```

### How they depend on each other

Compose `depends_on` is the boot order, not the data flow. Three services have **no** service dependencies and can start in parallel: **postgres**, **neo4j**, **kafka**.

```mermaid
flowchart TB
  PG[postgres healthy]
  N4J[neo4j healthy]
  K[kafka healthy]

  PG --> KC[kafka-connect]
  K --> KC
  PG --> DI[debezium-init]
  KC --> DI

  PG --> PS[prefect-server]
  PS --> PW[prefect-worker]
  PG --> PW
  N4J --> PW

  K --> BR[outbox-bridge]
  PW --> BR
  DI -->|"must exit 0"| BR

  PG --> APP[app]
  N4J --> APP

  style DI fill:#f0f0f0,stroke:#333
  style BR fill:#ffe6a0,stroke:#333
  style APP fill:#90EE90,stroke:#333
```

| Waits for | Why |
|-----------|-----|
| **kafka-connect** → postgres + kafka | Connector reads WAL and writes topics |
| **debezium-init** → kafka-connect + postgres | REST register; **exits** when the `ontology-outbox` connector exists (201) or already exists (409) |
| **prefect-server** → postgres | Metadata DB `prefect` (created by `infra/postgres/init-prefect-db.sql`) |
| **prefect-worker** → prefect-server + postgres + neo4j | Needs API, outbox rows, and Bolt |
| **outbox-bridge** → kafka + prefect-worker + **debezium-init success** | No point consuming an empty topic or triggering a pool that is not registered |
| **app** → postgres + neo4j | Dual store. It does **not** wait for Kafka/Prefect; first writes land in Postgres even if projection lags |

Startup in practice looks like this:

```mermaid
sequenceDiagram
  participant PG as postgres
  participant N4J as neo4j
  participant K as kafka
  participant KC as kafka-connect
  participant DI as debezium-init
  participant PS as prefect-server
  participant PW as prefect-worker
  participant BR as outbox-bridge
  participant APP as app

  par data plane
    PG->>PG: ontology + prefect DBs
    N4J->>N4J: Bolt + browser
    K->>K: KRaft broker
  end
  KC->>K: produce CDC
  DI->>KC: register ontology-outbox
  DI-->>DI: exit 0
  PS->>PG: Prefect metadata
  PW->>PS: deploy flows, start pool
  BR->>K: subscribe ontology.public.outbox
  APP->>PG: dual writes
  APP->>N4J: graph reads
```

### Two runtime paths (same stack)

**Path A — analysis (you click in the UI)**

```text
Browser :8501
  → app (LangGraph)
      → postgres  INSERT entities / relationships / outbox
      → neo4j     traverse (after drain_outbox)
      → briefing  stays in LangGraph state, not a new container
```

**Path B — keep Neo4j in sync (background)**

```text
postgres outbox INSERT (status=pending)
  → WAL  → kafka-connect (Debezium)
  → kafka topic ontology.public.outbox
  → outbox-bridge   filter: create, or update back to pending
  → prefect-server  flow run project-outbox-row
  → prefect-worker  claim row → MERGE Neo4j → mark done
```

If Path B drops a message, **prefect-worker** still runs `reconcile-pending-outbox` every 5 minutes and re-triggers leftover `pending` rows. That is why bridge and worker are split: Kafka is the fast trigger; Prefect is the durable executor.

```mermaid
sequenceDiagram
  participant APP as app
  participant PG as postgres
  participant KC as kafka-connect
  participant K as kafka
  participant BR as outbox-bridge
  participant PF as prefect-server
  participant W as prefect-worker
  participant N4J as neo4j

  APP->>PG: upsert entity + INSERT outbox pending
  PG-->>KC: WAL change
  KC->>K: Debezium op=c after.status=pending
  K->>BR: consume
  BR->>PF: create_flow_run outbox_id
  PF->>W: project-outbox-row
  W->>PG: claim processing
  W->>N4J: MERGE node/edge
  W->>PG: mark done
```

### Ports and credentials (local)

| Where | URL | Login |
|-------|-----|--------|
| Demo UI | http://localhost:8501 | — |
| Prefect | http://localhost:4200 | — |
| Neo4j Browser | http://localhost:7474 | `neo4j` / `ontology-dev` |
| Postgres | `localhost:5432` | `ontology` / `ontology` — databases `ontology` and `prefect` |
| Kafka | `localhost:9092` | plaintext |
| Connect REST | http://localhost:8083 | — |

## Demo Scenario: Taiwan Strait Supply Chain Disruption

The system analyzes a realistic geopolitical scenario:

- **50+ entities**: TSMC, ASML, Apple, NVIDIA, US Pacific Fleet, PLA Navy, shipping companies, ports, military assets, threat vectors
- **100+ relationships**: Supply chains, dependencies, military deployments, threat connections
- **4 specialist agents** run in order so writes land before traversal:
  1. **OSINT Collector** searches for current intelligence (Tavily web search or demo data) and updates the shared graph
  2. **Graph Analyst** traverses that same graph for dependency chains and exposure scores
  3. **Threat Assessor** evaluates risk with confidence scoring and historical precedents
  4. **Briefing Drafter** generates a structured executive briefing

## Ontology

The typed ontology layer is what agents coordinate *over*. It is not a document store:

- **Entity types**: Organization, Person, Location, Event, Asset, Threat
- **Relationship types**: OPERATES_IN, SUPPLIES, THREATENS, DEPENDS_ON, DEPLOYED_AT, MONITORS, and 13 more
- **Graph traversal**: N-hop queries, dependency chain discovery, exposure scoring
- **Shortest path**: BFS in memory/Postgres; Cypher `shortestPath` on Neo4j
- **Persistence**: pluggable backends behind `OntologyStore` (see below)
- **UI**: interactive Streamlit graph (`streamlit-agraph`) on the shared store; OSINT nodes highlighted

That is why a question like "does a Kaohsiung blockade hit Apple?" is answered by walking the graph in the [example above](#what-that-sentence-means), not by hoping the model remembers a supply-chain article. How the two live agents actually read and write this layer is in [How OSINT and Graph Analyst use the ontology](#how-osint-and-graph-analyst-use-the-ontology).

## Persistence and visualization

`OntologyStore` is a facade. Agents never issue SQL or Cypher.

| `ONTOLOGY_BACKEND` | Role |
|--------------------|------|
| `memory` | Default for local runs and tests. In-process graph. |
| `postgres` | System of record: `entities`, `relationships`, schema in `migrations/001_init.sql`. Traversal uses SQL neighbors + shared BFS. |
| `neo4j` | Graph projection: typed labels and relationship types. `find_path` uses Cypher. |
| `dual` | Compose default. **Authoritative writes** go to Postgres plus an **outbox row** in one transaction. Neo4j is updated asynchronously (see below). Entity reads use Postgres; graph traversal calls `drain_outbox()` then Neo4j, or falls back to Postgres if projection is behind. |

### Dual mode: outbox and Neo4j projection

Postgres remains the source of truth. Neo4j is a read-optimized projection for traversal and Cypher shortest paths. Which Compose container owns each hop is in [Compose microservices](#compose-microservices).

```mermaid
flowchart LR
  subgraph write["Write path (app)"]
    A[OSINT / API] --> PG[(Postgres entities + outbox)]
  end

  subgraph async["Async projection (Compose)"]
    PG -->|WAL CDC| DBZ[Debezium]
    DBZ --> K[ontology.public.outbox]
    K --> BR[outbox-bridge]
    BR --> PF[Prefect project-outbox-row]
    PF --> N4J[(Neo4j MERGE)]
  end

  subgraph fallback["Fallback"]
    CRON[Prefect reconcile every 5m] --> PF
  end

  subgraph read["Read path"]
    GA[Graph Analyst] --> DR[drain_outbox]
    DR --> N4J
    DR -.->|lag / error| PG
  end
```

| Stage | Component | Behavior |
|-------|-----------|----------|
| 1 | `PostgresBackend` | Same transaction: upsert entity/relationship + `INSERT outbox` (`status=pending`) |
| 2 | Debezium | Logical replication on `public.outbox` → Kafka |
| 3 | `outbox-bridge` | On INSERT or retry-to-`pending`, triggers Prefect deployment `project-outbox-row/project-outbox-row` |
| 4 | `OutboxProjector` | Claim row → `MERGE` into Neo4j → mark `done` |
| 5 | `reconcile-pending-outbox` | Scheduled flow; re-triggers runs for any still-`pending` rows |
| 6 | LangGraph nodes | `store.drain_outbox()` before graph ops — **read-your-writes** within a run |

**Sync vs async flush**

| `OUTBOX_SYNC_FLUSH` | When to use |
|---------------------|-------------|
| `false` (Compose default) | Production-like path: Kafka + Prefect worker stack |
| `true` | Local debugging without workers; every write calls `flush_outbox()` in-process |

Schema: `migrations/001_init.sql` (core tables), `migrations/002_outbox_processing.sql` (claim columns, Debezium publication). Worker image: `Dockerfile.worker` + `requirements-outbox.txt`. Deployments: `prefect.yaml`.

Empty stores are seeded once from `load_sample_data()`. The Streamlit checkbox **Show Ontology Graph** renders the live shared graph (orange = this-process OSINT writes; larger nodes = hubs or high exposure after a run).

### Environment variables (dual / workers)

See `.env.example`. Common settings:

| Variable | Default (Compose) | Purpose |
|----------|-------------------|---------|
| `ONTOLOGY_BACKEND` | `dual` | Store backend |
| `OUTBOX_SYNC_FLUSH` | `false` | Synchronous Neo4j flush on each write |
| `DATABASE_URL` | Postgres ontology DB | PG DSN |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | bolt + credentials | Neo4j projection |
| `PREFECT_API_URL` | `http://prefect-server:4200/api` | Bridge + worker |
| `PREFECT_OUTBOX_DEPLOYMENT` | `project-outbox-row/project-outbox-row` | Deployment to trigger |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | Bridge consumer |
| `KAFKA_OUTBOX_TOPIC` | `ontology.public.outbox` | Debezium topic |
| `OUTBOX_RECONCILE_BATCH` | `200` | Reconcile flow batch size |
| `OUTBOX_LOCK_MINUTES` | `5` | Stale `processing` reclaim |

## Project Structure

```
src/
  orchestrator.py              -- Coordinator agent, routes tasks
  ontology/
    schema.py                  -- Typed entity and relationship dataclasses
    store.py                   -- Facade used by agents
    factory.py                 -- get_shared_store() / ONTOLOGY_BACKEND
    memory.py                  -- In-memory backend
    postgres_backend.py        -- Postgres + transactional outbox
    neo4j_backend.py           -- Neo4j graph backend + project_* helpers
    dual.py                    -- PG authoritative + Neo4j projection
    outbox_projector.py        -- Claim outbox rows, MERGE to Neo4j
    loader.py                  -- 50+ entity Taiwan Strait scenario data
  workers/
    prefect_flows.py           -- project-outbox-row + reconcile-pending-outbox
    prefect_client.py          -- Trigger deployment runs from bridge/reconcile
    kafka_bridge.py            -- Consume Debezium topic → Prefect
    kafka_cdc.py               -- CDC payload filters (testable, no Kafka import)
  agents/                      -- OSINT, Graph, Threat, Briefing
  graph/                       -- LangGraph state, nodes, workflow
  tools/                       -- Web search, ontology tools, threat mocks
  ui/app.py                    -- Streamlit demo + graph visualization
infra/
  debezium/postgres-outbox.json   -- Kafka Connect connector config
  postgres/init-prefect-db.sql    -- Prefect metadata database (first init)
migrations/                  -- 001 core schema, 002 outbox CDC/claim
scripts/
  prefect-worker-entrypoint.sh    -- Deploy flows + start worker pool
  register-debezium.sh            -- Register connector on Connect startup
prefect.yaml                 -- Prefect deployment definitions
Dockerfile                   -- Streamlit app
Dockerfile.worker            -- Prefect worker + Kafka bridge
requirements.txt             -- App dependencies
requirements-outbox.txt      -- Prefect, confluent-kafka, asyncpg
tests/
config/                      -- Agent configs and system prompts
docker-compose.yml           -- Full stack (app + PG + Neo4j + Kafka + Prefect)
```

## Adjacent-Industry Precedents

This project draws structural inspiration from:

- **Anduril Lattice** -- AI agent orchestration for defense decision-making
- **Scale AI Donovan** -- Government intelligence analysis with multi-agent workflows
- **Databricks LakehouseIQ** -- Ontology-structured data analysis with AI agents

## Tech Stack

- **LangGraph** — Sequential StateGraph: OSINT then Graph Analyst on one store
- **LangChain + ChatOpenAI** — OpenAI-compatible API for agent reasoning
- **Tavily** — Real-time web search for OSINT
- **Streamlit + streamlit-agraph** — Demo UI and interactive ontology graph
- **Postgres** — System of record, transactional outbox (`pgoutput` publication for CDC)
- **Neo4j** — Graph projection for traversal and Cypher paths
- **Kafka (single node)** + **Debezium** — Outbox change capture
- **Prefect 3 (self-hosted)** — Per-row projection flows + scheduled reconcile
- **Python dataclasses** — Typed ontology schema

### Running workers locally (optional)

With Postgres, Neo4j, Kafka, and Prefect already up via Compose:

```bash
pip install -r requirements.txt -r requirements-outbox.txt
export PREFECT_API_URL=http://localhost:4200/api
export DATABASE_URL=postgresql://ontology:ontology@localhost:5432/ontology
export NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=ontology-dev

# Terminal A: Prefect worker (from repo root)
prefect work-pool create default-process-pool --type process 2>/dev/null || true
prefect deploy --all --prefect-file prefect.yaml
prefect worker start --pool default-process-pool

# Terminal B: Kafka bridge
python -m src.workers.kafka_bridge
```

Register Debezium once Connect is up: `KAFKA_CONNECT_URL=http://localhost:8083 ./scripts/register-debezium.sh`

## About

Built by **Hashwanth Sutharapu** -- contributor to [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) (8K+ stars) and [awesome-copilot](https://github.com/nicepkg/awesome-copilot) (29K+ stars). SDE at MAQ Software (Microsoft Partner), Bellevue WA.

---

*This is a technical demonstration. No classified data is used. All scenario data is derived from public sources.*
