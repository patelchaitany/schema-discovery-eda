import json
import os
import re
import subprocess
import textwrap

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
2. Statistical summaries (computed via native SQL / MongoDB aggregation) for each table

Perform a thorough Exploratory Data Analysis:
- Assess data types — flag any that look miscategorized
- Identify primary keys, foreign keys, and join paths
- Flag PII columns (names, emails, phones, etc.)
- Analyze the statistical summaries — flag outliers, skewed distributions, suspicious min/max values
- Suggest data quality checks (nulls, duplicates, range violations)
- Recommend visualizations based on the actual data distributions
- Highlight any cross-table relationships visible from the stats

Be specific to the actual table names, column names, and statistics provided."""

FEATURE_SUGGESTION_PROMPT = """You are a senior ML engineer. You will receive a database schema, statistical summaries, and a use-case description.

Suggest features that would be useful for the given use-case. For each feature, provide:
- name: snake_case feature name
- description: what this feature captures and why it matters for the use-case
- entity: the primary key / entity column (e.g. "customer_id")
- source_table: which table the feature is derived from
- columns: list of source columns used
- transformation: SQL-like description of how to compute it
- dtype: the Feast value type (one of: INT64, FLOAT64, STRING, BOOL, UNIX_TIMESTAMP)

Return ONLY a JSON array. No explanation, no markdown fences, no text before or after.
Example format:
[
  {
    "name": "customer_transaction_count_7d",
    "description": "Number of transactions in the last 7 days",
    "entity": "customer_id",
    "source_table": "transactions",
    "columns": ["customer_id", "timestamp"],
    "transformation": "COUNT(*) WHERE timestamp > now() - 7 days GROUP BY customer_id",
    "dtype": "INT64"
  }
]

Suggest 8-15 diverse features covering different aspects of the use-case.
Use actual table names and column names from the provided schema."""


# --- Type Classification ---

PG_NUMERIC_TYPES = frozenset({
    'integer', 'bigint', 'smallint', 'numeric', 'real', 'double precision',
})
PG_TEXT_TYPES = frozenset({
    'character varying', 'text', 'character',
})
PG_DATE_TYPES = frozenset({
    'date', 'timestamp without time zone', 'timestamp with time zone',
})
PG_BOOL_TYPES = frozenset({'boolean'})


def _to_float(val):
    if val is None:
        return None
    return round(float(val), 4)


# --- Schema Discovery (PostgreSQL — native SQL stats) ---

def _pg_table_stats(cur, table_name: str, columns: dict[str, str]) -> dict:
    """Single native SQL query per table: COUNT, AVG, STDDEV, MIN/MAX,
    PERCENTILE_CONT (25/50/75), COUNT(DISTINCT), MODE()."""
    numeric_cols = [c for c, t in columns.items() if t.lower() in PG_NUMERIC_TYPES]
    text_cols = [c for c, t in columns.items() if t.lower() in PG_TEXT_TYPES]
    date_cols = [c for c, t in columns.items() if t.lower() in PG_DATE_TYPES]
    bool_cols = [c for c, t in columns.items() if t.lower() in PG_BOOL_TYPES]

    parts = []
    for col in columns:
        q = f'"{col}"'
        parts.append(f'COUNT({q}) AS "{col}|count"')

        if col in numeric_cols:
            parts.extend([
                f'AVG({q}) AS "{col}|mean"',
                f'STDDEV({q}) AS "{col}|std"',
                f'MIN({q}) AS "{col}|min"',
                f'PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {q}) AS "{col}|25%"',
                f'PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY {q}) AS "{col}|50%"',
                f'PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {q}) AS "{col}|75%"',
                f'MAX({q}) AS "{col}|max"',
            ])
        elif col in text_cols or col in bool_cols:
            parts.extend([
                f'COUNT(DISTINCT {q}) AS "{col}|unique"',
                f'MODE() WITHIN GROUP (ORDER BY {q}) AS "{col}|top"',
            ])
        elif col in date_cols:
            parts.extend([
                f'MIN({q})::text AS "{col}|min"',
                f'MAX({q})::text AS "{col}|max"',
            ])

    query = f'SELECT {", ".join(parts)} FROM "{table_name}"'
    cur.execute(query)

    row = cur.fetchone()
    aliases = [desc[0] for desc in cur.description]
    raw = dict(zip(aliases, row))

    stats: dict[str, dict] = {}
    for col in columns:
        s: dict = {"count": raw.get(f"{col}|count")}

        if col in numeric_cols:
            for key in ("mean", "std", "min", "25%", "50%", "75%", "max"):
                s[key] = _to_float(raw.get(f"{col}|{key}"))
        elif col in text_cols or col in bool_cols:
            s["unique"] = raw.get(f"{col}|unique")
            top = raw.get(f"{col}|top")
            s["top"] = str(top) if top is not None else None
        elif col in date_cols:
            s["min"] = raw.get(f"{col}|min")
            s["max"] = raw.get(f"{col}|max")

        stats[col] = s

    for col in text_cols + bool_cols:
        top_val = stats[col].get("top")
        if top_val is not None:
            cur.execute(
                f'SELECT COUNT(*) FROM "{table_name}" WHERE "{col}"::text = %s',
                (top_val,),
            )
            stats[col]["freq"] = cur.fetchone()[0]

    return stats


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
    for table_name, columns in tables.items():
        stats[table_name] = _pg_table_stats(cur, table_name, columns)

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
        f"## Statistical Summaries (Native SQL / Aggregation)\n```json\n{json.dumps(stats, indent=2)}\n```\n\n"
        "Perform a thorough EDA analysis."
    )


def stream_llm(prompt: str, sys_prompt: str, base_url: str, api_key: str, model: str):
    client = OpenAI(base_url=base_url, api_key=api_key)
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": sys_prompt},
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


# --- Feature Engineering Helpers ---

FEAST_DTYPE_MAP = {
    "INT64": "Int64",
    "FLOAT64": "Float64",
    "STRING": "String",
    "BOOL": "Bool",
    "UNIX_TIMESTAMP": "UnixTimestamp",
}


def parse_feature_json(text: str) -> list[dict]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("No JSON array found in LLM response")
    return json.loads(text[start : end + 1])


def stream_suggest_features(
    schema: dict, stats: dict, use_case: str,
    base_url: str, api_key: str, model: str,
):
    prompt = (
        f"## Use Case\n{use_case}\n\n"
        f"## Schema\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
        f"## Statistical Summaries\n```json\n{json.dumps(stats, indent=2)}\n```"
    )
    client = OpenAI(base_url=base_url, api_key=api_key)
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": FEATURE_SUGGESTION_PROMPT},
            {"role": "user", "content": prompt},
        ],
        stream=True,
    )
    for chunk in stream:
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def _parse_pg_conn_string(conn_str: str) -> dict:
    parts = {}
    for token in conn_str.split():
        if "=" in token:
            k, v = token.split("=", 1)
            parts[k] = v
    return {
        "host": parts.get("host", "localhost"),
        "port": parts.get("port", "5432"),
        "database": parts.get("dbname", "postgres"),
        "user": parts.get("user", "postgres"),
        "password": parts.get("password", ""),
    }


def generate_feature_repo(
    features: list[dict], connections: list[dict], repo_dir: str,
) -> list[str]:
    os.makedirs(os.path.join(repo_dir, "data"), exist_ok=True)
    created = []

    pg_conn = next(
        (c for c in connections if c["type"] == "PostgreSQL" and c["conn"].strip()),
        None,
    )
    pg_params = _parse_pg_conn_string(pg_conn["conn"]) if pg_conn else {
        "host": "localhost", "port": "5432", "database": "postgres",
        "user": "postgres", "password": "postgres",
    }

    yaml_content = textwrap.dedent(f"""\
        project: feature_repo
        provider: local
        registry:
          type: file
          path: data/registry.db
        online_store:
          type: sqlite
          path: data/online_store.db
        offline_store:
          type: postgres
          host: {pg_params['host']}
          port: {pg_params['port']}
          database: {pg_params['database']}
          db_schema: public
          user: {pg_params['user']}
          password: {pg_params['password']}
        entity_key_serialization_version: 2
    """)
    yaml_path = os.path.join(repo_dir, "feature_store.yaml")
    with open(yaml_path, "w") as f:
        f.write(yaml_content)
    created.append("feature_store.yaml")

    entities = {}
    views_by_table = {}
    for feat in features:
        entity = feat["entity"]
        if entity not in entities:
            entities[entity] = feat.get("source_table", "unknown")
        table = feat["source_table"]
        views_by_table.setdefault(table, []).append(feat)

    lines = [
        "from datetime import timedelta",
        "",
        "from feast import Entity, FeatureService, FeatureView, Field",
        "from feast.infra.offline_stores.contrib.postgres_offline_store.postgres_source import (",
        "    PostgreSQLSource,",
        ")",
        "from feast.types import Float64, Int64, String, Bool, UnixTimestamp",
        "",
    ]

    for ent_name in entities:
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", ent_name)
        lines.append(f'{safe} = Entity(name="{ent_name}", join_keys=["{ent_name}"])')
    lines.append("")

    view_names = []
    for table, feats in views_by_table.items():
        source_var = f"{table}_source"
        lines.append(f'{source_var} = PostgreSQLSource(')
        lines.append(f'    name="{table}_source",')
        lines.append(f'    query="SELECT * FROM {table}",')
        lines.append(f'    timestamp_field="created_at",')
        lines.append(f")")
        lines.append("")

        view_name = f"{table}_features"
        view_names.append(view_name)
        entity_name = feats[0]["entity"]
        safe_entity = re.sub(r"[^a-zA-Z0-9_]", "_", entity_name)

        field_lines = []
        for feat in feats:
            dtype = FEAST_DTYPE_MAP.get(feat.get("dtype", "FLOAT64"), "Float64")
            field_lines.append(f'        Field(name="{feat["name"]}", dtype={dtype}),')

        lines.append(f"{view_name} = FeatureView(")
        lines.append(f'    name="{view_name}",')
        lines.append(f"    entities=[{safe_entity}],")
        lines.append(f"    ttl=timedelta(days=1),")
        lines.append(f"    schema=[")
        lines.extend(field_lines)
        lines.append(f"    ],")
        lines.append(f"    source={source_var},")
        lines.append(f")")
        lines.append("")

    lines.append("feature_service = FeatureService(")
    lines.append('    name="feature_service",')
    lines.append(f"    features=[{', '.join(view_names)}],")
    lines.append(")")
    lines.append("")

    features_path = os.path.join(repo_dir, "features.py")
    with open(features_path, "w") as f:
        f.write("\n".join(lines))
    created.append("features.py")

    return created


def run_feast_apply(repo_dir: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["feast", "-c", repo_dir, "apply"],
        capture_output=True, text=True, timeout=120,
    )
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr
    return result.returncode == 0, output


def generate_sdk_snippets(repo_dir: str, features: list[dict]) -> str:
    feature_refs = []
    views_seen = set()
    for feat in features:
        view_name = f"{feat['source_table']}_features"
        if view_name not in views_seen:
            views_seen.add(view_name)
        feature_refs.append(f'    "{view_name}:{feat["name"]}",')

    refs_str = "\n".join(feature_refs)
    entity_name = features[0]["entity"] if features else "entity_id"

    return textwrap.dedent(f"""\
        from feast import FeatureStore
        import pandas as pd

        # Initialize the feature store
        store = FeatureStore(repo_path="{repo_dir}")

        # --- Get Historical Features (for training) ---
        entity_df = pd.DataFrame({{
            "{entity_name}": [1, 2, 3],
            "event_timestamp": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
        }})

        feature_refs = [
        {refs_str}
        ]

        training_df = store.get_historical_features(
            entity_df=entity_df,
            features=feature_refs,
        ).to_df()
        print(training_df.head())

        # --- Get Online Features (for inference) ---
        online_features = store.get_online_features(
            features=feature_refs,
            entity_rows=[{{"{entity_name}": 1}}, {{"{entity_name}": 2}}],
        ).to_dict()
        print(online_features)

        # --- Materialize (push offline features to online store) ---
        from datetime import datetime, timedelta

        store.materialize(
            start_date=datetime.now() - timedelta(days=7),
            end_date=datetime.now(),
        )
    """)


# --- Streamlit UI ---

st.set_page_config(page_title="Schema Discovery + EDA", layout="wide")
st.title("Schema Discovery + LLM EDA")

# Sidebar: LLM config
with st.sidebar:
    st.header("LLM Configuration")
    llm_url = st.text_input("LLM Base URL", value="http://localhost:11434/v1")
    llm_key = st.text_input("API Key", value="ollama")
    llm_model = st.text_input("Model", value="llama3.1:8b")
    st.header("System Prompt")
    system_prompt = st.text_area("Edit the system prompt", value=SYSTEM_PROMPT, height=300)

# Connection strings
st.header("Data Sources")
PG_DEFAULT = "host=localhost port=5432 dbname=banking_db user=postgres password=postgres"
MONGO_DEFAULT = "mongodb://localhost:27017"
MONGO_DB_DEFAULT = "banking_mongo"

if "connections" not in st.session_state:
    st.session_state.connections = [
        {"type": "PostgreSQL", "conn": PG_DEFAULT, "db": ""},
        {"type": "MongoDB", "conn": MONGO_DEFAULT, "db": MONGO_DB_DEFAULT},
    ]

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
    tab_schema, tab_stats, tab_prompt, tab_eda, tab_feat = st.tabs([
        "Schema", "Statistics (Native SQL)", "LLM Prompt", "LLM EDA Analysis",
        "Feature Engineering",
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
        st.code(system_prompt, language="text")
        st.subheader("User Prompt")
        st.code(st.session_state.results_prompt, language="markdown")

    with tab_eda:
        if st.session_state.get("run_eda"):
            try:
                st.session_state.results_eda = st.write_stream(
                    stream_llm(st.session_state.results_prompt, system_prompt, llm_url, llm_key, llm_model)
                )
            except Exception as e:
                st.error(f"LLM error: {e}")
            st.session_state.run_eda = False
        elif "results_eda" in st.session_state:
            st.markdown(st.session_state.results_eda)
        else:
            st.info("Click 'Discover Schemas & Run EDA' to start.")

    with tab_feat:
        st.subheader("Feature Engineering")
        use_case = st.text_input(
            "Use Case",
            placeholder="e.g., fraud detection, customer churn prediction, credit risk scoring",
            key="feature_use_case_input",
        )

        if st.button("Suggest Features", type="primary", disabled=not use_case.strip()):
            try:
                table_placeholder = st.empty()
                raw_expander = st.expander("Show raw LLM output", expanded=False)
                raw_text_placeholder = raw_expander.empty()

                full_text = ""
                parsed_features = []
                brace_depth = 0
                in_string = False
                escape_next = False
                obj_start = -1

                col_order = ["name", "description", "entity", "source_table",
                             "columns", "transformation", "dtype"]

                for token in stream_suggest_features(
                    st.session_state.results_schema,
                    st.session_state.results_stats,
                    use_case,
                    llm_url, llm_key, llm_model,
                ):
                    full_text += token
                    raw_text_placeholder.code(full_text, language="json")

                    for ch in token:
                        if escape_next:
                            escape_next = False
                            continue
                        if ch == "\\" and in_string:
                            escape_next = True
                            continue
                        if ch == '"':
                            in_string = not in_string
                            continue
                        if in_string:
                            continue
                        if ch == "{":
                            if brace_depth == 0:
                                obj_start = full_text.rfind("{")
                            brace_depth += 1
                        elif ch == "}":
                            brace_depth -= 1
                            if brace_depth == 0 and obj_start != -1:
                                obj_text = full_text[obj_start:full_text.rfind("}") + 1]
                                try:
                                    feat = json.loads(obj_text)
                                    if isinstance(feat.get("columns"), list):
                                        feat["columns"] = ", ".join(feat["columns"])
                                    parsed_features.append(feat)
                                    df = pd.DataFrame(parsed_features)
                                    display_cols = [c for c in col_order if c in df.columns]
                                    table_placeholder.dataframe(
                                        df[display_cols],
                                        hide_index=True,
                                        use_container_width=True,
                                    )
                                except (json.JSONDecodeError, KeyError):
                                    pass
                                obj_start = -1

                for feat in parsed_features:
                    feat["selected"] = True
                st.session_state.suggested_features = parsed_features
                st.session_state.feature_repo_generated = False
                st.session_state.feast_applied = False
            except Exception as e:
                st.error(f"Feature suggestion failed: {e}")

        if "suggested_features" in st.session_state:
            st.divider()
            st.markdown("**Review suggested features** — uncheck any you want to exclude:")

            display_df = pd.DataFrame(st.session_state.suggested_features)
            col_order = ["selected", "name", "description", "entity", "source_table",
                         "columns", "transformation", "dtype"]
            col_order = [c for c in col_order if c in display_df.columns]
            display_df = display_df[col_order]

            edited = st.data_editor(
                display_df,
                column_config={
                    "selected": st.column_config.CheckboxColumn("Select", default=True),
                    "name": st.column_config.TextColumn("Feature Name", disabled=True),
                    "description": st.column_config.TextColumn("Description", disabled=True, width="large"),
                    "entity": st.column_config.TextColumn("Entity", disabled=True),
                    "source_table": st.column_config.TextColumn("Source Table", disabled=True),
                    "columns": st.column_config.TextColumn("Source Columns", disabled=True),
                    "transformation": st.column_config.TextColumn("Transformation", disabled=True, width="large"),
                    "dtype": st.column_config.TextColumn("Type", disabled=True),
                },
                hide_index=True,
                use_container_width=True,
                key="feature_editor",
            )

            selected = edited[edited["selected"] == True]
            total = len(edited)
            st.caption(f"{len(selected)} of {total} features selected")

            st.divider()

            repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feature_repo")

            if st.button(
                "Generate Feature Repo",
                type="primary",
                disabled=len(selected) == 0,
            ):
                sel_features = selected.drop(columns=["selected"]).to_dict("records")
                for feat in sel_features:
                    if isinstance(feat.get("columns"), str):
                        feat["columns"] = [c.strip() for c in feat["columns"].split(",")]
                with st.spinner("Generating feature repo..."):
                    try:
                        created = generate_feature_repo(
                            sel_features,
                            st.session_state.connections,
                            repo_dir,
                        )
                        st.session_state.feature_repo_generated = True
                        st.session_state.selected_features = sel_features
                        st.success(f"Feature repo created at `{repo_dir}/`")
                        st.markdown("**Generated files:**")
                        for f in created:
                            st.markdown(f"- `{f}`")
                    except Exception as e:
                        st.error(f"Generation failed: {e}")

            if st.session_state.get("feature_repo_generated"):
                with st.expander("View generated feature_store.yaml"):
                    yaml_path = os.path.join(repo_dir, "feature_store.yaml")
                    if os.path.exists(yaml_path):
                        with open(yaml_path) as f:
                            st.code(f.read(), language="yaml")

                with st.expander("View generated features.py"):
                    py_path = os.path.join(repo_dir, "features.py")
                    if os.path.exists(py_path):
                        with open(py_path) as f:
                            st.code(f.read(), language="python")

                st.divider()

                if st.button("Run Feast Apply", type="primary"):
                    with st.spinner("Running `feast apply`..."):
                        try:
                            success, output = run_feast_apply(repo_dir)
                            st.session_state.feast_applied = success
                            if success:
                                st.success("Feast apply completed successfully!")
                            else:
                                st.error("Feast apply failed.")
                            with st.expander("Command output", expanded=not success):
                                st.code(output, language="text")
                        except Exception as e:
                            st.error(f"Feast apply error: {e}")

                if st.session_state.get("feast_applied"):
                    st.divider()
                    st.subheader("SDK Code Snippets")
                    st.markdown("Use the following code to interact with your feature store:")
                    snippets = generate_sdk_snippets(
                        repo_dir, st.session_state.selected_features,
                    )
                    st.code(snippets, language="python")
