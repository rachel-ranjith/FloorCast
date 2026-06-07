# Floorcast

A B2B SaaS tool for data centre ops teams: a **rack replacement optimizer** and
**floor visualizer**. Configure your topology, visualize the floor as a power
heatmap, and run an ILP engine that produces a 12-month rack replacement schedule
while keeping every power tier within its configured headroom.

## Capabilities

- Configure topology: **buildings → suites → rows → rack positions**
- Define custom rack types, generations, and power draws (config-driven)
- Visualize the floor as a heatmap by power draw
- Run an **OR-Tools ILP** optimizer that generates a 12-month replacement schedule
- Maintain power-headroom constraints through **every** swap, at every tier
- Simulate what-if scenarios via per-run config overrides

## Stack

| Layer       | Tech                                              |
|-------------|---------------------------------------------------|
| Backend     | Python + FastAPI                                  |
| Optimizer   | OR-Tools (ILP / CBC MIP)                           |
| Live state  | DynamoDB (topology + rack fleet)                  |
| Analytics   | Aurora PostgreSQL (runs, schedules, history)      |
| Frontend    | Next.js *(later)*                                 |
| Deployment  | AWS Lambda (Mangum) + Vercel                      |

## Project layout

```
floorcast/
├── config/
│   ├── floorcast.yaml          # ALL constraints, limits, rack catalog (not hardcoded)
│   └── settings.py             # YAML + env loader (pydantic-settings)
├── src/floorcast/
│   ├── models/                 # pydantic domain models
│   ├── db/
│   │   ├── dynamo/             # table def, client, repository (live state)
│   │   └── aurora/             # schema.sql, SQLAlchemy models, session
│   ├── optimizer/              # OR-Tools ILP engine
│   ├── services/               # generator, optimization, heatmap orchestration
│   ├── api/                    # FastAPI app + routers
│   └── lambda_handler.py       # Mangum adapter for AWS Lambda
├── scripts/
│   ├── create_dynamo_tables.py
│   ├── apply_aurora_schema.py
│   └── seed_data.py            # generates the fake data centre
├── tests/
├── docker-compose.yml          # DynamoDB Local + Postgres
└── Makefile
```

## Configuration is the source of truth

Every constraint — headroom %, building/suite/row MW limits, rack power draws,
swap throughput caps, the optimization horizon — lives in
[`config/floorcast.yaml`](config/floorcast.yaml). Environment variables
(`FLOORCAST_*`, nested via `__`) override any value. Nothing is hardcoded in the
optimizer or power model. What-if scenarios pass a partial `config_overrides`
dict that is deep-merged on top of the base config per run.

## Quickstart (local)

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
make install

# 2. Start local infra (DynamoDB Local + Postgres)
make dev-up
cp .env.example .env        # already points at localhost

# 3. Create storage
make create-tables
make apply-schema

# 4. Seed a realistic fake data centre (2 buildings x 4 suites x 48 rows)
make seed
#   or, no AWS needed — preview to JSON + load report:
make seed-dry

# 5. Run the API
make api      # http://localhost:8000/docs
```

### Try the optimizer

```bash
curl -X POST localhost:8000/optimize \
  -H 'content-type: application/json' \
  -d '{"name": "baseline Q3 plan"}'

# What-if: tighten headroom to 30%
curl -X POST localhost:8000/optimize \
  -H 'content-type: application/json' \
  -d '{"name": "tight headroom", "config_overrides": {"power": {"headroom_pct": 0.30}}}'
```

## Data model

**DynamoDB** (single-table, live state) — buildings, suites, rows, positions,
and racks share one table; GSIs serve heatmap-by-power and fleet-by-generation
queries. See [`src/floorcast/db/dynamo/tables.py`](src/floorcast/db/dynamo/tables.py).

**Aurora PostgreSQL** (system of record for decisions) — `optimization_runs`,
`schedules`, `schedule_items`, `power_utilization`, `scenarios`, `rack_history`.
Each run snapshots its fully-resolved config so results are reproducible. See
[`src/floorcast/db/aurora/schema.sql`](src/floorcast/db/aurora/schema.sql).

## Tests

```bash
make test
```

Covers config loading + env overrides, the topology generator, and the optimizer
(including a check that headroom is **never** violated in any month, at any tier).
