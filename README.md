# Schema Discovery + LLM EDA

Scan PostgreSQL and MongoDB databases, discover table schemas, compute statistical summaries (`df.describe`), and get LLM-powered Exploratory Data Analysis -- all through a Streamlit UI or standalone scripts.

## Demo

<video src="demo/schema_discovery_eda_demo.mp4" controls width="100%">
  <a href="demo/schema_discovery_eda_demo.mp4">Download demo video</a>
</video>

## Setup

```bash
cd schema_discovery_eda
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## Prerequisites

- **PostgreSQL** running on `localhost:5432`
- **MongoDB** running on `localhost:27017`
- **Ollama** running on `localhost:11434` (or any OpenAI-compatible LLM server)

Quick start with Docker:

```bash
docker run -d --name postgres_banking -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=banking_db -p 5432:5432 postgres:16
docker run -d --name mongo_banking -p 27017:27017 mongo:7
```

## Usage

### Streamlit UI (recommended)

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`. Features:

- Add multiple database connections (PostgreSQL / MongoDB)
- View discovered schemas in table or JSON format
- View `df.describe` statistics for every table
- Editable system prompt in the sidebar
- Streaming LLM response for EDA analysis
- View the exact prompt sent to the LLM

### CLI Scripts

#### 1. Seed test data (banking use case)

```bash
python setup_test_data.py
```

Creates `banking_db` in PostgreSQL (5 tables) and `banking_mongo` in MongoDB (5 collections) with sample banking data.

#### 2. Discover schemas

```bash
python schema_discovery.py                    # saves to schema_output.json
python schema_discovery.py my_output.json     # saves to custom path
```

#### 3. Run EDA via LLM

```bash
python llm_eda_request.py                     # reads schema_output.json
python llm_eda_request.py my_output.json      # reads custom path
```

Edit `OGX_BASE_URL` and `MODEL` at the top of `llm_eda_request.py` if your LLM server or model differs.

## Project Structure

```
schema_discovery_eda/
├── app.py                 # Streamlit UI
├── schema_discovery.py    # Scan PostgreSQL + MongoDB schemas
├── llm_eda_request.py     # Send schema to LLM for EDA (CLI)
├── setup_test_data.py     # Seed both databases with banking test data
├── demo/                  # README demo video (MP4)
├── requirements.txt       # Dependencies
└── README.md
```
