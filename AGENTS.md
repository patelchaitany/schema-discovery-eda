# Schema Discovery + LLM EDA — Agent Instructions

## Project Overview

This project scans PostgreSQL and MongoDB databases, discovers table/collection schemas, computes `df.describe` statistics, and sends everything to a local LLM for Exploratory Data Analysis. It has a Streamlit UI and standalone CLI scripts.

## Setup Commands

Run these in order. Every command runs from the project root (`schema_discovery_eda/`).

### 1. Create virtual environment and install dependencies

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 2. Start databases via Docker

```bash
docker run -d --name postgres_banking -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=banking_db -p 5432:5432 postgres:16
docker run -d --name mongo_banking -p 27017:27017 mongo:7
```

Wait a few seconds for PostgreSQL to initialize, then seed:

```bash
.venv/bin/python setup_test_data.py
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
.venv/bin/streamlit run app.py --server.headless true
```

Opens at `http://localhost:8501`. The app starts with default connections pre-filled:
- PostgreSQL: `host=localhost port=5432 dbname=banking_db user=postgres password=postgres`
- MongoDB: `mongodb://localhost:27017` / database: `banking_mongo`

Click "Discover Schemas & Run EDA" to run the full pipeline.

### 4b. Run via CLI (alternative)

```bash
.venv/bin/python schema_discovery.py
.venv/bin/python llm_eda_request.py
```

## Default Connection Strings

| Database   | Connection String | Database Name |
|------------|-------------------|---------------|
| PostgreSQL | `host=localhost port=5432 dbname=banking_db user=postgres password=postgres` | banking_db |
| MongoDB    | `mongodb://localhost:27017` | banking_mongo |

## File Descriptions

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI — connection management, schema/stats/prompt/EDA tabs, streaming LLM response, editable system prompt |
| `schema_discovery.py` | Scan PostgreSQL (`information_schema.columns`) and MongoDB (document sampling) for schemas, output JSON |
| `llm_eda_request.py` | CLI script — send schema JSON to LLM via OpenAI-compatible API |
| `setup_test_data.py` | Seed both databases with banking test data (5 tables each, 20-50 rows per table) |
| `requirements.txt` | Python dependencies: `psycopg2-binary`, `pymongo`, `openai`, `streamlit`, `pandas` |

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

## Troubleshooting

- **Docker containers stopped**: `docker start postgres_banking mongo_banking`
- **Port conflict on 5432/27017**: Stop existing services or change port mappings in the docker run commands
- **Ollama not responding**: Run `ollama serve` or check if another process uses port 11434
- **LLM timeout**: The EDA response can take 30-60 seconds on smaller models. The streaming UI shows progress in real time.
- **"No module named X"**: Make sure you activated the venv: `source .venv/bin/activate`
