import json
import streamlit as st
import psycopg2
import pandas as pd
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
from openai import OpenAI

BSON_TYPE_MAP = {
    str: "string", int: "int", float: "double", bool: "bool",
    list: "array", dict: "object", datetime: "datetime",
    ObjectId: "ObjectId", type(None): "null",
}

SYSTEM_PROMPT = """You are a senior data analyst. You will receive:
1. A database schema (table/collection names, column names, data types)
2. Statistical summaries (df.describe output) for each table

Perform a thorough Exploratory Data Analysis:
- Assess data types — flag any that look miscategorized
- Identify primary keys, foreign keys, and join paths
- Flag PII columns (names, emails, phones, etc.)
- Analyze the statistical summaries — flag outliers, skewed distributions, suspicious min/max values
- Suggest data quality checks (nulls, duplicates, range violations)
- Recommend visualizations based on the actual data distributions
- Highlight any cross-table relationships visible from the stats

Be specific to the actual table names, column names, and statistics provided."""


# --- Schema Discovery ---

def discover_postgres(conn_string: str) -> tuple[list[dict], dict]:
    conn = psycopg2.connect(conn_string)
    cur = conn.cursor()

    cur.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """)
    tables: dict[str, dict[str, str]] = {}
    for table, column, dtype in cur.fetchall():
        tables.setdefault(table, {})[column] = dtype

    schema = [{name: cols} for name, cols in tables.items()]

    stats = {}
    for table_name in tables:
        df = pd.read_sql(f'SELECT * FROM "{table_name}"', conn)
        stats[table_name] = json.loads(df.describe(include="all").to_json())

    cur.close()
    conn.close()
    return schema, stats


def discover_mongodb(uri: str, db_name: str) -> tuple[list[dict], dict]:
    client = MongoClient(uri)
    db = client[db_name]
    collections = []
    stats = {}

    for col_name in sorted(db.list_collection_names()):
        field_types: dict[str, str] = {}
        docs = list(db[col_name].find().limit(100))
        for doc in docs:
            for key, value in doc.items():
                if key not in field_types:
                    field_types[key] = BSON_TYPE_MAP.get(type(value), type(value).__name__)
        collections.append({col_name: field_types})

        flat_docs = []
        for doc in docs:
            flat = {}
            for k, v in doc.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    flat[k] = v
            flat_docs.append(flat)
        if flat_docs:
            df = pd.DataFrame(flat_docs)
            stats[col_name] = json.loads(df.describe(include="all").to_json())

    client.close()
    return collections, stats


def build_prompt(schema: dict, stats: dict) -> str:
    return (
        f"## Schema\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
        f"## Statistical Summaries (df.describe)\n```json\n{json.dumps(stats, indent=2)}\n```\n\n"
        "Perform a thorough EDA analysis."
    )


def stream_llm(prompt: str, base_url: str, api_key: str, model: str):
    client = OpenAI(base_url=base_url, api_key=api_key)
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        stream=True,
    )
    for chunk in stream:
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def schema_to_tables(schema: dict) -> dict[str, pd.DataFrame]:
    tables = {}
    for db_name, table_list in schema.items():
        for table_dict in table_list:
            for table_name, columns in table_dict.items():
                rows = [{"Column": col, "Data Type": dtype} for col, dtype in columns.items()]
                tables[f"{db_name}.{table_name}"] = pd.DataFrame(rows)
    return tables


# --- Streamlit UI ---

st.set_page_config(page_title="Schema Discovery + EDA", layout="wide")
st.title("Schema Discovery + LLM EDA")

# Sidebar: LLM config
with st.sidebar:
    st.header("LLM Configuration")
    llm_url = st.text_input("LLM Base URL", value="http://localhost:11434/v1")
    llm_key = st.text_input("API Key", value="ollama")
    llm_model = st.text_input("Model", value="llama3.1:8b")

# Connection strings
st.header("Data Sources")
if "connections" not in st.session_state:
    st.session_state.connections = [{"type": "PostgreSQL", "conn": "", "db": ""}]

def add_connection():
    st.session_state.connections.append({"type": "PostgreSQL", "conn": "", "db": ""})

def remove_connection(idx):
    st.session_state.connections.pop(idx)

for i, conn in enumerate(st.session_state.connections):
    col1, col2, col3, col4 = st.columns([1, 3, 2, 0.5])
    with col1:
        st.session_state.connections[i]["type"] = st.selectbox(
            "Type", ["PostgreSQL", "MongoDB"], key=f"type_{i}",
            index=0 if conn["type"] == "PostgreSQL" else 1
        )
    with col2:
        st.session_state.connections[i]["conn"] = st.text_input(
            "Connection String", value=conn["conn"], key=f"conn_{i}",
            placeholder="host=localhost port=5432 dbname=banking_db user=postgres password=postgres"
            if conn["type"] == "PostgreSQL" else "mongodb://localhost:27017"
        )
    with col3:
        if st.session_state.connections[i]["type"] == "MongoDB":
            st.session_state.connections[i]["db"] = st.text_input(
                "Database Name", value=conn["db"], key=f"db_{i}",
                placeholder="banking_mongo"
            )
        else:
            st.empty()
    with col4:
        if len(st.session_state.connections) > 1:
            st.button("X", key=f"rm_{i}", on_click=remove_connection, args=(i,))

st.button("+ Add Connection", on_click=add_connection)

# Run
if st.button("Discover Schemas & Run EDA", type="primary"):
    all_schema = {}
    all_stats = {}
    errors = []

    with st.spinner("Scanning databases..."):
        for conn_cfg in st.session_state.connections:
            if not conn_cfg["conn"].strip():
                continue
            try:
                if conn_cfg["type"] == "PostgreSQL":
                    schema, stats = discover_postgres(conn_cfg["conn"])
                    db_name = next(
                        (p.split("=")[1] for p in conn_cfg["conn"].split() if p.startswith("dbname=")),
                        "postgres"
                    )
                    all_schema[db_name] = schema
                    all_stats[db_name] = stats
                else:
                    db_name = conn_cfg["db"] or "mongodb"
                    schema, stats = discover_mongodb(conn_cfg["conn"], db_name)
                    all_schema[db_name] = schema
                    all_stats[db_name] = stats
            except Exception as e:
                errors.append(f"{conn_cfg['type']} ({conn_cfg['conn'][:40]}...): {e}")

    if errors:
        for err in errors:
            st.error(err)

    if all_schema:
        st.session_state.results_schema = all_schema
        st.session_state.results_stats = all_stats
        st.session_state.results_prompt = build_prompt(all_schema, all_stats)
        st.session_state.run_eda = True

# Display results if available
if "results_schema" in st.session_state:
    tab_schema, tab_stats, tab_prompt, tab_eda = st.tabs([
        "Schema", "Statistics (df.describe)", "LLM Prompt", "LLM EDA Analysis"
    ])

    with tab_schema:
        view = st.radio("View as", ["Table", "JSON"], horizontal=True, key="schema_view")
        if view == "Table":
            for full_name, df_table in schema_to_tables(st.session_state.results_schema).items():
                st.write(f"**{full_name}**")
                st.dataframe(df_table, hide_index=True, width="stretch")
        else:
            st.json(st.session_state.results_schema)

    with tab_stats:
        for db_name, tables in st.session_state.results_stats.items():
            st.subheader(db_name)
            for table_name, desc in tables.items():
                st.write(f"**{table_name}**")
                df_desc = pd.DataFrame(desc).astype(str).replace("nan", "")
                st.dataframe(df_desc, width="stretch")

    with tab_prompt:
        st.subheader("System Prompt")
        st.code(SYSTEM_PROMPT, language="text")
        st.subheader("User Prompt")
        st.code(st.session_state.results_prompt, language="markdown")

    with tab_eda:
        if st.session_state.get("run_eda"):
            try:
                st.session_state.results_eda = st.write_stream(
                    stream_llm(st.session_state.results_prompt, llm_url, llm_key, llm_model)
                )
            except Exception as e:
                st.error(f"LLM error: {e}")
            st.session_state.run_eda = False
        elif "results_eda" in st.session_state:
            st.markdown(st.session_state.results_eda)
        else:
            st.info("Click 'Discover Schemas & Run EDA' to start.")
