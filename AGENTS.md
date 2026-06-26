# Schema Discovery + LLM EDA + Feature Engineering — Agent Instructions

## Project Overview

This project scans PostgreSQL and MongoDB databases, discovers table/collection schemas, computes native SQL statistics, and sends everything to a local LLM for Exploratory Data Analysis. After EDA, users can enter a use-case (e.g., "fraud detection") to get LLM-suggested features, select/deselect them in a table, generate a Feast feature repo in one click, run `feast apply`, and get SDK code snippets. It has a Streamlit UI and standalone CLI scripts.

## Setup Commands

Run these in order. Every command runs from the project root (`schema_discovery_eda/`).

### 1. Install dependencies with uv

```bash
uv sync
```

### 2. Start databases via Docker

```bash
docker run -d --name banking_postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=banking_db -p 5432:5432 postgres:16-alpine
docker run -d --name banking_mongo -p 27017:27017 mongo:7
```

Wait a few seconds for PostgreSQL to initialize, then seed:

```bash
uv run python setup_test_data.py
```

Expected output:
```
PostgreSQL: banking_db seeded with 5 tables.
MongoDB: banking_mongo seeded with 5 collections.
```

### 3. Start Ollama (LLM)

Ollama must be running with at least one model available. If not already running:

```bash
ollama serve &
```

Check available models:

```bash
curl -s http://localhost:11434/api/tags | python -m json.tool
```

If no model is available, pull one:

```bash
ollama pull llama3.1:8b
```

### 4. Run the Streamlit app

```bash
uv run streamlit run app.py --server.headless true
```

Opens at `http://localhost:8501`. The app starts with default connections pre-filled:
- PostgreSQL: `host=localhost port=5432 dbname=banking_db user=postgres password=postgres`
- MongoDB: `mongodb://localhost:27017` / database: `banking_mongo`

Click "Discover Schemas & Run EDA" to run the full pipeline.

### 4b. Run via CLI (alternative)

```bash
uv run python schema_discovery.py
uv run python llm_eda_request.py
```

## Default Connection Strings

| Database   | Connection String | Database Name |
|------------|-------------------|---------------|
| PostgreSQL | `host=localhost port=5432 dbname=banking_db user=postgres password=postgres` | banking_db |
| MongoDB    | `mongodb://localhost:27017` | banking_mongo |

## File Descriptions

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI — connection management, schema/stats/prompt/EDA tabs, Feature Engineering tab (LLM feature suggestion with streaming row-by-row table, Feast repo generation, feast apply, SDK snippets) |
| `schema_discovery.py` | Scan PostgreSQL (`information_schema.columns`) and MongoDB (document sampling) for schemas, output JSON |
| `llm_eda_request.py` | CLI script — send schema JSON to LLM via OpenAI-compatible API |
| `setup_test_data.py` | Seed both databases with banking test data (5 tables each, 20-50 rows per table) |
| `pyproject.toml` | uv project config with dependencies: `psycopg2-binary`, `pymongo`, `openai`, `streamlit`, `pandas`, `feast[postgres]` |

## LLM Configuration

The app uses Ollama's OpenAI-compatible endpoint by default:
- **Base URL**: `http://localhost:11434/v1`
- **API Key**: `ollama`
- **Model**: `llama3.1:8b`

These are editable in the Streamlit sidebar. Any OpenAI-compatible server works (OGX, vLLM, etc.) — just change the URL and model name.

## Test Data Schema

### PostgreSQL (`banking_db`) — 5 tables

- `customers` — id, first_name, last_name, email, phone, date_of_birth, address, created_at
- `accounts` — id, customer_id (FK), account_type, balance, currency, opened_date, status
- `transactions` — id, account_id (FK), transaction_type, amount, timestamp, description, status
- `loans` — id, customer_id (FK), principal_amount, interest_rate, term_months, start_date, status
- `credit_cards` — id, customer_id (FK), card_number, credit_limit, current_balance, expiry_date, is_active

### MongoDB (`banking_mongo`) — 5 collections

- `customer_profiles` — name, email, phone, date_of_birth, address (nested), kyc_verified, preferences (nested)
- `transaction_logs` — customer_email, type, amount, currency, timestamp, metadata (nested: device, ip, location)
- `fraud_alerts` — customer_email, alert_type, risk_score, details (nested), resolved
- `branch_info` — branch_name, branch_code, location (nested: city, lat, lon), services (array), operating_hours (nested)
- `audit_trails` — user, action, timestamp, details (nested: user_agent, ip), status

## Feature Engineering Flow

The **Feature Engineering** tab (5th tab) provides a full pipeline from EDA to Feast feature store:

1. **Suggest Features** — Enter a use-case (e.g., "fraud detection") and click "Suggest Features". The LLM streams its response and features appear row-by-row in a table as they are generated. Raw LLM output is hidden behind a collapsed expander.

2. **Review & Select** — After streaming completes, an editable table appears with checkboxes. Uncheck features you don't want. Shows "X of Y features selected".

3. **Generate Feature Repo** — Click "Generate Feature Repo" to create `feature_repo/` with:
   - `feature_store.yaml` — Feast config pointing to the user's PostgreSQL (parsed from existing connection)
   - `features.py` — Entity, FeatureView, FeatureService, and PostgreSQLSource definitions
   - Expandable previews of both generated files

4. **Run Feast Apply** — Click "Run Feast Apply" to execute `feast apply` via subprocess. Shows command output in an expander.

5. **SDK Code Snippets** — On success, displays ready-to-use Python code for `get_historical_features()`, `get_online_features()`, and `materialize()`.

### Generated File Structure

```
feature_repo/
├── feature_store.yaml    # Feast configuration
├── features.py           # Entity, FeatureView, FeatureService definitions
└── data/                 # Created by feast apply (gitignored)
    ├── registry.db
    └── online_store.db
```

## Troubleshooting

- **Docker containers stopped**: `docker start banking_postgres banking_mongo`
- **Port conflict on 5432/27017**: Stop existing services or change port mappings in the docker run commands
- **Ollama not responding**: Run `ollama serve` or check if another process uses port 11434
- **LLM timeout**: The EDA response can take 30-60 seconds on smaller models. The streaming UI shows progress in real time.
- **Feature suggestion fails to parse**: The LLM sometimes wraps JSON in markdown fences or adds explanation text. The parser handles both cases, but very small models may produce invalid JSON. Try a larger model.
- **Feast apply fails**: Ensure PostgreSQL is running and accessible. Feast uses PostgreSQL as the offline store — the connection details are parsed from the Data Sources section.
- **"No module named X"**: Run `uv sync` to install all dependencies.
