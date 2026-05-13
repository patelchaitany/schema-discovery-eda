# Schema Discovery + LLM EDA

Scan PostgreSQL and MongoDB databases, discover schemas, then send the schema to an LLM via OGX for Exploratory Data Analysis.

## Setup

```bash
cd schema_discovery_eda
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## Prerequisites

- PostgreSQL running on `localhost:5432`
- MongoDB running on `localhost:27017`
- OGX server running on `localhost:8321` (with a model available, e.g. via Ollama)

## Usage

### 1. Seed test data (banking use case)

```bash
python setup_test_data.py
```

Creates `banking_db` in PostgreSQL (5 tables) and `banking_mongo` in MongoDB (5 collections).

### 2. Discover schemas

```bash
python schema_discovery.py                    # saves to schema_output.json
python schema_discovery.py my_output.json     # saves to custom path
```

### 3. Run EDA via LLM

```bash
python llm_eda_request.py                         # reads schema_output.json
python llm_eda_request.py my_output.json           # reads custom path
```

Edit `OGX_BASE_URL` and `MODEL` at the top of `llm_eda_request.py` if your OGX server or model differs.
