import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.request
import urllib.error

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
- depends_on: list of feature names (from this same list) that must be computed before this feature. Empty list if no dependencies.
- requires_creation: true if this feature does NOT already exist as a raw column and must be computed, false if it maps directly to an existing column

IMPORTANT: Order features so that parent features appear BEFORE any features that depend on them.

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
    "dtype": "INT64",
    "depends_on": [],
    "requires_creation": true
  },
  {
    "name": "customer_avg_txn_amount_7d",
    "description": "Average transaction amount in the last 7 days",
    "entity": "customer_id",
    "source_table": "transactions",
    "columns": ["customer_id", "amount", "timestamp"],
    "transformation": "AVG(amount) WHERE timestamp > now() - 7 days GROUP BY customer_id",
    "dtype": "FLOAT64",
    "depends_on": [],
    "requires_creation": true
  },
  {
    "name": "customer_txn_velocity_ratio",
    "description": "Ratio of 7-day txn count to average, indicating unusual activity spikes",
    "entity": "customer_id",
    "source_table": "transactions",
    "columns": ["customer_id"],
    "transformation": "customer_transaction_count_7d / NULLIF(customer_avg_txn_amount_7d, 0)",
    "dtype": "FLOAT64",
    "depends_on": ["customer_transaction_count_7d", "customer_avg_txn_amount_7d"],
    "requires_creation": true
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
                tables[f"{db_name}_{table_name}"] = pd.DataFrame(rows)
    return tables


# --- Feature Engineering Helpers ---

FEAST_DTYPE_MAP = {
    "INT64": "Int64",
    "FLOAT64": "Float64",
    "STRING": "String",
    "BOOL": "Bool",
    "UNIX_TIMESTAMP": "UnixTimestamp",
}

FEAST_SKILL = {
    "name": "feast-user-guide",
    "description": (
        "Guide for working with Feast (Feature Store) — defining features, "
        "configuring feature_store.yaml, retrieving features online/offline, "
        "using the CLI, and building RAG retrieval pipelines."
    ),
    "content": textwrap.dedent("""\
        # Feast User Guide

        ## Quick Start

        A Feast project requires:
        1. A `feature_store.yaml` config file
        2. Python files defining entities, data sources, feature views, and feature services
        3. Running `feast apply` to register definitions

        ```bash
        feast init my_project
        cd my_project
        feast apply
        ```

        ## Core Concepts

        ### Entity
        An entity is a collection of semantically related features (e.g., a customer, a driver).
        Entities have join keys used to look up features.

        ```python
        from feast import Entity
        from feast.value_type import ValueType

        driver = Entity(
            name="driver_id",
            description="Driver identifier",
            value_type=ValueType.INT64,
        )
        ```

        ### Data Sources
        Data sources describe where raw feature data lives.

        ```python
        from feast import FileSource, BigQuerySource, KafkaSource, PushSource, RequestSource
        from feast.data_format import ParquetFormat

        driver_stats_source = FileSource(
            name="driver_stats_source",
            path="data/driver_stats.parquet",
            timestamp_field="event_timestamp",
            created_timestamp_column="created",
        )

        input_request = RequestSource(
            name="vals_to_add",
            schema=[Field(name="val_to_add", dtype=Float64)],
        )
        ```

        ### FeatureView
        Maps features from a data source to entities with a schema, TTL, and online/offline settings.

        ```python
        from feast import FeatureView, Field
        from feast.types import Float32, Int64, String
        from datetime import timedelta

        driver_hourly_stats = FeatureView(
            name="driver_hourly_stats",
            entities=[driver],
            ttl=timedelta(days=365),
            schema=[
                Field(name="conv_rate", dtype=Float32),
                Field(name="acc_rate", dtype=Float32),
                Field(name="avg_daily_trips", dtype=Int64),
            ],
            online=True,
            source=driver_stats_source,
        )
        ```

        ### OnDemandFeatureView
        Computes features at request time from other feature views and/or request data.

        ```python
        from feast import on_demand_feature_view
        import pandas as pd

        @on_demand_feature_view(
            sources=[driver_hourly_stats, input_request],
            schema=[Field(name="conv_rate_plus_val", dtype=Float64)],
            mode="pandas",
        )
        def transformed_conv_rate(inputs: pd.DataFrame) -> pd.DataFrame:
            df = pd.DataFrame()
            df["conv_rate_plus_val"] = inputs["conv_rate"] + inputs["val_to_add"]
            return df
        ```

        ### FeatureService
        Groups features from multiple views for retrieval.

        ```python
        from feast import FeatureService

        driver_fs = FeatureService(
            name="driver_ranking",
            features=[driver_hourly_stats, transformed_conv_rate],
        )
        ```

        ## Feature Retrieval

        ### Online (low-latency)
        ```python
        from feast import FeatureStore

        store = FeatureStore(repo_path=".")

        features = store.get_online_features(
            features=[
                "driver_hourly_stats:conv_rate",
                "driver_hourly_stats:acc_rate",
            ],
            entity_rows=[{"driver_id": 1001}, {"driver_id": 1002}],
        ).to_dict()
        ```

        ### Historical (training data with point-in-time joins)
        ```python
        entity_df = pd.DataFrame({
            "driver_id": [1001, 1002],
            "event_timestamp": [datetime(2023, 1, 1), datetime(2023, 1, 2)],
        })

        training_df = store.get_historical_features(
            entity_df=entity_df,
            features=["driver_hourly_stats:conv_rate", "driver_hourly_stats:acc_rate"],
        ).to_df()
        ```

        Or use a FeatureService:
        ```python
        training_df = store.get_historical_features(
            entity_df=entity_df,
            features=driver_fs,
        ).to_df()
        ```

        ## Materialization

        Load features from offline store into online store:

        ```bash
        feast materialize 2023-01-01T00:00:00 2023-12-31T23:59:59
        feast materialize-incremental $(date -u +"%Y-%m-%dT%H:%M:%S")
        ```

        Python API:
        ```python
        from datetime import datetime
        store.materialize(start_date=datetime(2023, 1, 1), end_date=datetime(2023, 12, 31))
        store.materialize_incremental(end_date=datetime.utcnow())
        ```

        ## CLI Commands

        | Command | Purpose |
        |---------|---------|
        | `feast init [DIR]` | Create new feature repository |
        | `feast apply` | Register/update feature definitions |
        | `feast plan` | Preview changes without applying |
        | `feast materialize START END` | Materialize features to online store |
        | `feast materialize-incremental END` | Incremental materialization |
        | `feast entities list` | List registered entities |
        | `feast feature-views list` | List feature views |
        | `feast feature-services list` | List feature services |
        | `feast teardown` | Remove infrastructure resources |
        | `feast version` | Show SDK version |

        ## Vector Search / RAG

        ```python
        from feast.types import Array, Float32

        wiki_passages = FeatureView(
            name="wiki_passages",
            entities=[passage_entity],
            schema=[
                Field(name="passage_text", dtype=String),
                Field(
                    name="embedding",
                    dtype=Array(Float32),
                    vector_index=True,
                    vector_length=384,
                    vector_search_metric="COSINE",
                ),
            ],
            source=passages_source,
            online=True,
        )

        results = store.retrieve_online_documents(
            feature="wiki_passages:embedding",
            query=query_embedding,
            top_k=5,
        )
        ```

        ## feature_store.yaml Minimal Config

        ```yaml
        project: my_project
        registry: data/registry.db
        provider: local
        online_store:
          type: sqlite
          path: data/online_store.db
        ```

        ## Common Imports

        ```python
        from feast import (
            Entity, FeatureView, OnDemandFeatureView, FeatureService,
            Field, FileSource, RequestSource, FeatureStore,
        )
        from feast.on_demand_feature_view import on_demand_feature_view
        from feast.types import Float32, Float64, Int64, String, Bool, Array
        from feast.value_type import ValueType
        from datetime import timedelta
        ```
    """),
}


def parse_skill_frontmatter(text: str) -> dict:
    text = text.strip()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            front = parts[1].strip()
            body = parts[2].strip()
            name = ""
            description = ""
            for line in front.splitlines():
                line = line.strip()
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
            return {"name": name or "unnamed_skill", "description": description, "content": body}
    title_match = re.search(r"^#\s+(.+)", text, re.MULTILINE)
    name = title_match.group(1).strip() if title_match else "unnamed_skill"
    return {"name": name, "description": "", "content": text}


# --- Feature Dependency Utilities ---

def build_dependency_graph(features: list[dict]) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {f["name"]: [] for f in features}
    for feat in features:
        deps = feat.get("depends_on", [])
        if isinstance(deps, str):
            deps = [d.strip() for d in deps.split(",") if d.strip()]
        for parent in deps:
            if parent in graph:
                graph[parent].append(feat["name"])
    return graph


def get_all_descendants(graph: dict[str, list[str]], name: str) -> set[str]:
    visited = set()
    queue = list(graph.get(name, []))
    while queue:
        child = queue.pop(0)
        if child not in visited:
            visited.add(child)
            queue.extend(graph.get(child, []))
    return visited


def enforce_dependency_selection(
    features: list[dict], selected_names: set[str],
) -> tuple[set[str], list[str]]:
    by_name = {f["name"]: f for f in features}
    warnings = []
    changed = True
    while changed:
        changed = False
        for name in list(selected_names):
            feat = by_name.get(name)
            if not feat:
                continue
            deps = feat.get("depends_on", [])
            if isinstance(deps, str):
                deps = [d.strip() for d in deps.split(",") if d.strip()]
            for dep in deps:
                if dep in by_name and dep not in selected_names:
                    selected_names.discard(name)
                    warnings.append(
                        f"'{name}' deselected — depends on '{dep}' which is not selected."
                    )
                    changed = True
                    break
    return selected_names, warnings


def topological_sort_features(features: list[dict]) -> list[dict]:
    by_name = {f["name"]: f for f in features}
    in_degree: dict[str, int] = {f["name"]: 0 for f in features}
    graph = build_dependency_graph(features)

    for feat in features:
        deps = feat.get("depends_on", [])
        if isinstance(deps, str):
            deps = [d.strip() for d in deps.split(",") if d.strip()]
        for dep in deps:
            if dep in in_degree:
                in_degree[feat["name"]] += 1

    queue = [n for n, d in in_degree.items() if d == 0]
    result = []
    while queue:
        node = queue.pop(0)
        result.append(by_name[node])
        for child in graph.get(node, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    for feat in features:
        if feat not in result:
            result.append(feat)
    return result


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
    skills: list[dict] | None = None,
):
    skills_section = ""
    if skills:
        parts = []
        for s in skills:
            parts.append(f"### Skill: {s['name']}\n{s.get('description', '')}\n\n{s['content']}")
        skills_section = (
            "\n\n## Reference Skills\n"
            "Use these reference guides to inform your feature suggestions. "
            "Follow the patterns, naming conventions, and best practices described below.\n\n"
            + "\n\n---\n\n".join(parts)
        )
    prompt = (
        f"## Use Case\n{use_case}\n\n"
        f"## Schema\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
        f"## Statistical Summaries\n```json\n{json.dumps(stats, indent=2)}\n```"
        f"{skills_section}"
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


def _detect_timestamp_cols_for_tables(pg_conn_str: str, tables: list[str]) -> dict[str, str]:
    conn = psycopg2.connect(pg_conn_str)
    cur = conn.cursor()
    result = {}
    for table in tables:
        ts_col = _detect_timestamp_col(cur, table)
        result[table] = ts_col or "created_at"
    cur.close()
    conn.close()
    return result


def _introspect_table_columns(pg_conn_str: str, table_name: str, entity_col: str) -> list[str]:
    """Get the actual value column names from a table (excluding entity and created_at)."""
    try:
        conn = psycopg2.connect(pg_conn_str)
        cur = conn.cursor()
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (table_name,))
        all_cols = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return [c for c in all_cols if c != entity_col and c != "created_at"]
    except Exception:
        return []


def generate_feature_repo(
    features: list[dict], connections: list[dict], repo_dir: str,
    table_source_types: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Generate Feast repo files. Returns (created_files, view_map).
    view_map maps feature_name -> view_name for use in get_feature_refs."""
    os.makedirs(os.path.join(repo_dir, "data"), exist_ok=True)
    created = []
    source_types = table_source_types or {}

    pg_conn = next(
        (c for c in connections if c["type"] == "PostgreSQL" and c["conn"].strip()),
        None,
    )
    pg_params = _parse_pg_conn_string(pg_conn["conn"]) if pg_conn else {
        "host": "localhost", "port": "5432", "database": "postgres",
        "user": "postgres", "password": "postgres",
    }
    pg_conn_str = pg_conn["conn"] if pg_conn else ""

    # --- feature_store.yaml (PostgreSQL only) ---
    yaml_lines = [
        "project: feature_repo",
        "provider: local",
        "registry:",
        "  type: file",
        "  path: data/registry.db",
        "online_store:",
        "  type: sqlite",
        "  path: data/online_store.db",
        "offline_store:",
        "  type: postgres",
        f"  host: {pg_params['host']}",
        f"  port: {pg_params['port']}",
        f"  database: {pg_params['database']}",
        "  db_schema: public",
        f"  user: {pg_params['user']}",
        f"  password: {pg_params['password']}",
        "  sslmode: disable",
        "entity_key_serialization_version: 3",
    ]

    yaml_path = os.path.join(repo_dir, "feature_store.yaml")
    with open(yaml_path, "w") as f:
        f.write("\n".join(yaml_lines) + "\n")
    created.append("feature_store.yaml")

    # --- features.py ---
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
        "from feast import Entity, FeatureService, FeatureView, Field, ValueType",
        "from feast.infra.offline_stores.contrib.postgres_offline_store.postgres_source import (",
        "    PostgreSQLSource,",
        ")",
        "from feast.types import Float64, Int64, String, Bool, UnixTimestamp",
        "",
    ]

    for ent_name in entities:
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", ent_name)
        lines.append(f'{safe} = Entity(name="{ent_name}", join_keys=["{ent_name}"], value_type=ValueType.INT64)')
    lines.append("")

    ts_cols = {}
    pg_tables = [t for t in views_by_table if source_types.get(t, "postgresql") == "postgresql"]
    if pg_conn and pg_tables:
        try:
            ts_cols = _detect_timestamp_cols_for_tables(pg_conn["conn"], pg_tables)
        except Exception:
            pass

    view_names = []
    view_map: dict[str, str] = {}

    for table, feats in views_by_table.items():
        raw_feats = [f for f in feats if not f.get("requires_creation")]
        computed_feats = [f for f in feats if f.get("requires_creation")]

        if raw_feats:
            source_var = f"{table}_source"
            ts_field = ts_cols.get(table, "created_at")
            entity_col = raw_feats[0]["entity"]

            # Introspect actual columns from DB for computed tables
            if table.startswith("computed_") and pg_conn_str:
                actual_cols = _introspect_table_columns(pg_conn_str, table, entity_col)
                actual_col = actual_cols[0] if actual_cols else None
            else:
                actual_col = None

            if actual_col and len(raw_feats) == 1 and raw_feats[0]["name"] != actual_col:
                # Alias the real column to match the feature name
                feat_name = raw_feats[0]["name"]
                query = f'SELECT "{entity_col}", "{actual_col}" AS "{feat_name}", "created_at" FROM {table}'
            else:
                query = f"SELECT * FROM {table}"

            lines.append(f'{source_var} = PostgreSQLSource(')
            lines.append(f'    name="{table}_source",')
            lines.append(f'    query="{query}",')
            lines.append(f'    timestamp_field="{ts_field}",')
            lines.append(f")")
            lines.append("")

            view_name = f"{table}_features"
            view_names.append(view_name)
            safe_entity = re.sub(r"[^a-zA-Z0-9_]", "_", entity_col)

            field_lines = []
            for feat in raw_feats:
                dtype = FEAST_DTYPE_MAP.get(feat.get("dtype", "FLOAT64"), "Float64")
                field_lines.append(f'        Field(name="{feat["name"]}", dtype={dtype}),')
                view_map[feat["name"]] = view_name

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

        for feat in computed_feats:
            comp_table = f"computed_{feat['name']}"
            comp_source_var = f"{comp_table}_source"
            entity_col = feat["entity"]

            # Introspect the actual column name from the computed table
            if pg_conn_str:
                actual_cols = _introspect_table_columns(pg_conn_str, comp_table, entity_col)
                actual_col = actual_cols[0] if actual_cols else feat["name"]
            else:
                actual_col = feat["name"]

            if actual_col != feat["name"]:
                query = f'SELECT "{entity_col}", "{actual_col}" AS "{feat["name"]}", "created_at" FROM {comp_table}'
            else:
                query = f"SELECT * FROM {comp_table}"

            lines.append(f'{comp_source_var} = PostgreSQLSource(')
            lines.append(f'    name="{comp_table}_source",')
            lines.append(f'    query="{query}",')
            lines.append(f'    timestamp_field="created_at",')
            lines.append(f")")
            lines.append("")

            comp_view = f"{feat['name']}_view"
            view_names.append(comp_view)
            safe_entity = re.sub(r"[^a-zA-Z0-9_]", "_", entity_col)
            dtype = FEAST_DTYPE_MAP.get(feat.get("dtype", "FLOAT64"), "Float64")

            lines.append(f"{comp_view} = FeatureView(")
            lines.append(f'    name="{comp_view}",')
            lines.append(f"    entities=[{safe_entity}],")
            lines.append(f"    ttl=timedelta(days=1),")
            lines.append(f"    schema=[")
            lines.append(f'        Field(name="{feat["name"]}", dtype={dtype}),')
            lines.append(f"    ],")
            lines.append(f"    source={comp_source_var},")
            lines.append(f")")
            lines.append("")

            view_map[feat["name"]] = comp_view

    lines.append("feature_service = FeatureService(")
    lines.append('    name="feature_service",')
    lines.append(f"    features=[{', '.join(view_names)}],")
    lines.append(")")
    lines.append("")

    features_path = os.path.join(repo_dir, "features.py")
    with open(features_path, "w") as f:
        f.write("\n".join(lines))
    created.append("features.py")

    return created, view_map


_FEAST_CMD = [os.path.join(os.path.dirname(sys.executable), "feast")]


def run_feast_apply(repo_dir: str) -> tuple[bool, str]:
    result = subprocess.run(
        [*_FEAST_CMD, "-c", repo_dir, "apply"],
        capture_output=True, text=True, timeout=120,
    )
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr
    return result.returncode == 0, output


def run_feast_materialize(repo_dir: str, pg_conn_str: str | None = None) -> tuple[bool, str]:
    from datetime import timedelta as _td
    end = datetime.utcnow()
    start = end - _td(days=1)

    if pg_conn_str:
        try:
            conn = psycopg2.connect(pg_conn_str)
            cur = conn.cursor()
            # Dynamically find all computed_* tables and get their min created_at
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name LIKE 'computed_%'
            """)
            computed_tables = [row[0] for row in cur.fetchall()]
            if computed_tables:
                union_parts = [
                    f'SELECT MIN(created_at) AS min_ts FROM "{t}"'
                    for t in computed_tables
                ]
                cur.execute(f"SELECT MIN(min_ts) FROM ({' UNION ALL '.join(union_parts)}) sub")
                row = cur.fetchone()
                if row and row[0]:
                    start = row[0] - _td(days=1)
            cur.close()
            conn.close()
        except Exception:
            start = end - _td(days=1)

    result = subprocess.run(
        [
            *_FEAST_CMD, "-c", repo_dir, "materialize",
            start.strftime("%Y-%m-%dT%H:%M:%S"),
            end.strftime("%Y-%m-%dT%H:%M:%S"),
        ],
        capture_output=True, text=True, timeout=300,
    )
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr
    if result.returncode == 0:
        return True, f"Materialized features from {start.date()} to {end.date()}."
    return False, output


def get_feature_refs(features: list[dict], view_map: dict[str, str]) -> list[str]:
    """Build Feast feature references using the actual view_map from generate_feature_repo."""
    refs = []
    for feat in features:
        view_name = view_map.get(feat["name"])
        if view_name:
            refs.append(f"{view_name}:{feat['name']}")
    return refs


def _find_table_with_column(cur, column: str, candidate_tables: list[str]) -> str | None:
    for table in candidate_tables:
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """, (table, column))
        if cur.fetchone():
            return table
    return None


def run_get_historical_features(
    repo_dir: str, features: list[dict], pg_conn_str: str,
    view_map: dict[str, str] | None = None,
) -> pd.DataFrame | str:
    try:
        from feast import FeatureStore
        store = FeatureStore(repo_path=repo_dir)

        feature_refs = get_feature_refs(features, view_map or {})

        entities_to_tables: dict[str, list[str]] = {}
        for feat in features:
            ent = feat["entity"]
            entities_to_tables.setdefault(ent, [])
            if feat["source_table"] not in entities_to_tables[ent]:
                entities_to_tables[ent].append(feat["source_table"])

        entity_name = list(entities_to_tables.keys())[0]
        candidate_tables = entities_to_tables[entity_name]

        conn = psycopg2.connect(pg_conn_str)
        cur = conn.cursor()

        source_table = _find_table_with_column(cur, entity_name, candidate_tables)
        if not source_table:
            cur.execute("""
                SELECT table_name FROM information_schema.columns
                WHERE table_schema = 'public' AND column_name = %s
                LIMIT 1
            """, (entity_name,))
            row = cur.fetchone()
            source_table = row[0] if row else candidate_tables[0]

        cur.execute(f'SELECT DISTINCT "{entity_name}" FROM "{source_table}" LIMIT 20')
        entity_ids = [row[0] for row in cur.fetchall()]

        ts_col = _detect_timestamp_col(cur, source_table)
        if ts_col:
            cur.execute(f'SELECT MIN("{ts_col}"), MAX("{ts_col}") FROM "{source_table}"')
            ts_min, ts_max = cur.fetchone()
        else:
            ts_min = ts_max = None
        cur.close()
        conn.close()

        if not entity_ids:
            return "No entity rows found in the source table."

        has_computed = any(f.get("requires_creation") for f in features)
        if ts_min and ts_max and not has_computed:
            timestamps = [ts_max] * len(entity_ids)
        else:
            timestamps = [datetime.utcnow()] * len(entity_ids)

        entity_df = pd.DataFrame({
            entity_name: entity_ids,
            "event_timestamp": pd.to_datetime(timestamps),
        })

        training_df = store.get_historical_features(
            entity_df=entity_df,
            features=feature_refs,
        ).to_df()
        return training_df
    except Exception as e:
        return f"Error retrieving historical features: {e}"


def _detect_timestamp_col(cur, table_name: str) -> str | None:
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
          AND data_type IN ('timestamp without time zone', 'timestamp with time zone', 'date')
        ORDER BY ordinal_position
    """, (table_name,))
    candidates = [row[0] for row in cur.fetchall()]
    for preferred in ("created_at", "timestamp", "event_timestamp", "updated_at"):
        if preferred in candidates:
            return preferred
    return candidates[0] if candidates else None


def generate_sdk_snippets(repo_dir: str, features: list[dict], view_map: dict[str, str] | None = None) -> str:
    feature_refs = get_feature_refs(features, view_map or {})
    refs_str = "\n".join(f'    "{ref}",' for ref in feature_refs)
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


# --- Hoop Gateway Client ---

def hoop_execute(
    gateway_url: str, api_key: str, connection: str, script: str,
) -> tuple[bool, str]:
    url = f"{gateway_url.rstrip('/')}/api/sessions"
    payload = json.dumps({"connection": connection, "script": script, "type": "exec"}).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            if data.get("exit_code", 1) == 0:
                return True, data.get("output", "OK")
            return False, data.get("output", "Unknown error")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


# --- Transformation Generation ---

def _get_pg_context(pg_conn_str: str) -> str:
    try:
        conn = psycopg2.connect(pg_conn_str)
        cur = conn.cursor()

        cur.execute("""
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """)
        tables: dict[str, list[str]] = {}
        for table, col, dtype, nullable in cur.fetchall():
            null_str = "" if nullable == "YES" else " NOT NULL"
            tables.setdefault(table, []).append(f"  {col} {dtype}{null_str}")

        ddl_lines = []
        for table, cols in sorted(tables.items()):
            ddl_lines.append(f"CREATE TABLE {table} (\n" + ",\n".join(cols) + "\n);")

        cur.execute("""
            SELECT tc.table_name, kcu.column_name,
                   ccu.table_name AS foreign_table, ccu.column_name AS foreign_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
        """)
        fk_lines = []
        for table, col, ftable, fcol in cur.fetchall():
            fk_lines.append(f"  {table}.{col} → {ftable}.{fcol}")

        date_lines = []
        for table in tables:
            cur.execute(f"""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                  AND data_type IN ('timestamp without time zone', 'timestamp with time zone', 'date')
            """, (table,))
            for (col,) in cur.fetchall():
                try:
                    cur.execute(f'SELECT MIN("{col}")::text, MAX("{col}")::text FROM "{table}"')
                    mn, mx = cur.fetchone()
                    if mn and mx:
                        date_lines.append(f"  {table}.{col}: {mn} to {mx}")
                except Exception:
                    pass

        sample_lines = []
        for table in ["transactions", "accounts", "customers", "credit_cards", "loans"]:
            if table in tables:
                try:
                    cur.execute(f'SELECT * FROM "{table}" LIMIT 2')
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                    sample_lines.append(f"  {table} columns: {', '.join(cols)}")
                    for r in rows:
                        sample_lines.append(f"    {dict(zip(cols, r))}")
                except Exception:
                    pass

        cur.close()
        conn.close()

        sections = ["-- TABLE DDL --\n" + "\n\n".join(ddl_lines)]
        if fk_lines:
            sections.append("-- FOREIGN KEYS --\n" + "\n".join(fk_lines))
        if date_lines:
            sections.append("-- DATE RANGES --\n" + "\n".join(date_lines))
        if sample_lines:
            sections.append("-- SAMPLE ROWS --\n" + "\n".join(sample_lines))

        return "\n\n".join(sections)
    except Exception:
        return ""


TRANSFORMATION_SQL_PROMPT = """You are a PostgreSQL expert. Convert the given transformation description into valid executable SQL.

You will receive the EXACT database DDL, foreign keys, date ranges, and sample rows. Use ONLY the columns and tables shown — do NOT invent columns.

RULES:
- Output ONLY: DROP TABLE IF EXISTS computed_{feature_name}; then CREATE TABLE computed_{feature_name} AS SELECT ...
- The SELECT must produce exactly 3 columns: the entity column (e.g. customer_id), the computed value aliased AS feature_name, and NOW() AS created_at
- EVERY ENTITY MUST GET A ROW: Start from the entity table (e.g. customers) and LEFT JOIN to the source/aggregation table. Use COALESCE to provide sensible defaults for entities with no matching data:
  - Counts → COALESCE(..., 0)
  - Sums/averages/amounts → COALESCE(..., 0)
  - Booleans/flags → COALESCE(..., FALSE)
  - Ratios/rates → COALESCE(..., 0.0)
  This ensures every entity gets a feature value, not NULL.
- WHERE clause MUST come BEFORE GROUP BY
- Every non-aggregated column in SELECT MUST appear in GROUP BY
- Use CREATE TABLE ... AS SELECT (the AS keyword is required)
- Use STDDEV() not STDEV()
- Check the DDL: if the entity column does not exist in the source table, JOIN through the correct table using the foreign keys provided
- Check the DATE RANGES: if the data ends before today, do NOT use time filters like "now() - interval '7 days'" — use the actual date range or omit the filter
- For dependent features that reference other computed_* tables, JOIN those tables instead of the raw source table
- Return ONLY raw SQL. No explanation, no markdown fences, no comments."""


def _strip_sql_fences(text: str) -> str:
    text = re.sub(r"^```(?:sql)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _build_sql_prompt(
    feature: dict, pg_context: str, prior_sqls: list[dict] | None,
) -> str:
    date_hint = ""
    if pg_context and feature.get("source_table"):
        table = feature["source_table"]
        for line in pg_context.split("\n"):
            stripped = line.strip()
            if stripped.startswith(f"{table}.") and " to " in stripped:
                date_hint = (
                    f"\n\nCRITICAL: {stripped}. The data does NOT extend to today. "
                    f"Do NOT use NOW() or CURRENT_DATE in WHERE clauses — either use "
                    f"the actual max date from the range above, or omit the time filter entirely.\n"
                )
    prior_context = ""
    if prior_sqls:
        parts = [f"-- Feature: {p['name']}\n{p['sql']}" for p in prior_sqls]
        prior_context = (
            "\n\n## Previously generated computed tables\n"
            "These tables already exist. Use the EXACT column names shown here when referencing them.\n\n"
            + "\n\n".join(parts)
        )
    return (
        f"Feature name: {feature['name']}\n"
        f"Entity column: {feature['entity']}\n"
        f"Source table: {feature['source_table']}\n"
        f"Transformation: {feature['transformation']}\n\n"
        f"{pg_context}"
        f"{prior_context}"
        f"{date_hint}\n\n"
        "Convert this into valid PostgreSQL CREATE TABLE AS SELECT."
    )


def generate_and_execute_sql(
    feature: dict, pg_conn_str: str,
    base_url: str, api_key: str, model: str,
    pg_context: str = "", prior_sqls: list[dict] | None = None,
    max_retries: int = 3,
    on_attempt: callable = None,
    hoop_config: dict | None = None,
) -> tuple[bool, str, str | None]:
    """Generate SQL via LLM, execute it, retry on failure with error context.
    Returns (success, sql_or_error, last_error_if_failed)."""
    client = OpenAI(base_url=base_url, api_key=api_key)
    prompt = _build_sql_prompt(feature, pg_context, prior_sqls)

    messages = [
        {"role": "system", "content": TRANSFORMATION_SQL_PROMPT},
        {"role": "user", "content": prompt},
    ]
    resp = client.chat.completions.create(model=model, messages=messages)
    sql = _strip_sql_fences(resp.choices[0].message.content)

    for attempt in range(max_retries):
        if on_attempt:
            on_attempt(attempt, sql)
        if hoop_config:
            ok, err = hoop_execute(
                hoop_config["gateway_url"],
                hoop_config["api_key"],
                hoop_config["connection"],
                sql,
            )
        else:
            ok, err = pg_execute_direct(pg_conn_str, sql)
        if ok:
            return True, sql, None
        if attempt == max_retries - 1:
            return False, sql, err
        # Feed error back to LLM for correction
        messages.append({"role": "assistant", "content": sql})
        messages.append({"role": "user", "content": (
            f"This SQL failed with error:\n{err}\n\n"
            f"Fix the SQL and return ONLY the corrected SQL. No explanation."
        )})
        resp = client.chat.completions.create(model=model, messages=messages)
        sql = _strip_sql_fences(resp.choices[0].message.content)

    return False, sql, err


def pg_execute_direct(conn_str: str, sql: str) -> tuple[bool, str]:
    try:
        conn = psycopg2.connect(conn_str)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql)
        cur.close()
        conn.close()
        return True, "OK"
    except Exception as e:
        return False, str(e)


# --- Streamlit UI ---

st.set_page_config(page_title="Schema Discovery + EDA", layout="wide")
st.title("Schema Discovery + LLM EDA")

# Sidebar: LLM config + Skills
with st.sidebar:
    st.header("LLM Configuration")
    llm_url = st.text_input("LLM Base URL", value="http://localhost:11434/v1")
    llm_key = st.text_input("API Key", value="ollama")
    llm_model = st.text_input("Model", value="nemotron-3-ultra:cloud")

    st.header("Transformation LLM")
    transform_model = st.text_input(
        "Model for SQL/Mongo conversion", value="llama3.1:8b",
        help="Cheap local model that converts the transformation field into valid executable SQL or MongoDB pipelines.",
    )

    st.header("Hoop Gateway")
    hoop_enabled = st.toggle("Route writes through Hoop", value=False,
        help="When enabled, CREATE TABLE transformations execute via Hoop gateway instead of direct PostgreSQL.")
    if hoop_enabled:
        hoop_url = st.text_input("Gateway URL", value="http://localhost:8009")
        hoop_api_key = st.text_input("Hoop API Key", value="", type="password")
        hoop_pg_conn = st.text_input("PG Connection Name", value="pg-banking")
    else:
        hoop_url = ""
        hoop_api_key = ""
        hoop_pg_conn = ""

    sidebar_prompt_tab, sidebar_skills_tab = st.tabs(["System Prompt", "Skills"])

    with sidebar_prompt_tab:
        system_prompt = st.text_area("Edit the system prompt", value=SYSTEM_PROMPT, height=300)

    with sidebar_skills_tab:
        st.caption("Upload or paste skill guides (markdown with optional frontmatter).")

        if "skills" not in st.session_state:
            st.session_state.skills = [dict(FEAST_SKILL)]

        uploaded = st.file_uploader(
            "Upload skill (.md)", type=["md", "txt"], key="skill_uploader",
        )
        if uploaded is not None:
            raw = uploaded.read().decode("utf-8")
            parsed = parse_skill_frontmatter(raw)
            if not any(s["name"] == parsed["name"] for s in st.session_state.skills):
                st.session_state.skills.append(parsed)
                st.rerun()

        for idx, skill in enumerate(st.session_state.skills):
            with st.expander(skill["name"], expanded=False):
                st.markdown(f"**{skill.get('description', '')}**")
                st.session_state.skills[idx]["name"] = st.text_input(
                    "Name", value=skill["name"], key=f"skill_name_{idx}",
                )
                st.session_state.skills[idx]["description"] = st.text_area(
                    "Description", value=skill.get("description", ""),
                    key=f"skill_desc_{idx}", height=68,
                )
                st.session_state.skills[idx]["content"] = st.text_area(
                    "Content", value=skill["content"],
                    key=f"skill_content_{idx}", height=300,
                )
                if st.button("Remove", key=f"skill_rm_{idx}"):
                    st.session_state.skills.pop(idx)
                    st.rerun()

        with st.expander("Paste a new skill", expanded=False):
            pasted = st.text_area(
                "Paste skill markdown (with or without frontmatter)",
                key="skill_paste_input", height=200,
            )
            if st.button("Add Skill", key="skill_paste_btn") and pasted.strip():
                parsed = parse_skill_frontmatter(pasted)
                if not any(s["name"] == parsed["name"] for s in st.session_state.skills):
                    st.session_state.skills.append(parsed)
                    st.rerun()
                else:
                    st.warning(f"Skill '{parsed['name']}' already exists.")

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
    table_source_types: dict[str, str] = {}
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
                    for tbl_dict in schema:
                        for tbl_name in tbl_dict:
                            table_source_types[tbl_name] = "postgresql"
                else:
                    db_name = conn_cfg["db"] or "mongodb"
                    schema, stats = discover_mongodb(conn_cfg["conn"], db_name)
                    all_schema[db_name] = schema
                    all_stats[db_name] = stats
                    for tbl_dict in schema:
                        for tbl_name in tbl_dict:
                            table_source_types[tbl_name] = "mongodb"
            except Exception as e:
                errors.append(f"{conn_cfg['type']} ({conn_cfg['conn'][:40]}...): {e}")

    if errors:
        for err in errors:
            st.error(err)

    if all_schema:
        st.session_state.results_schema = all_schema
        st.session_state.results_stats = all_stats
        st.session_state.results_prompt = build_prompt(all_schema, all_stats)
        st.session_state.table_source_types = table_source_types
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
                             "columns", "transformation", "dtype",
                             "depends_on", "requires_creation"]

                for token in stream_suggest_features(
                    st.session_state.results_schema,
                    st.session_state.results_stats,
                    use_case,
                    llm_url, llm_key, llm_model,
                    skills=st.session_state.get("skills", []),
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
                                    if isinstance(feat.get("depends_on"), list):
                                        feat["depends_on"] = ", ".join(feat["depends_on"])
                                    feat.setdefault("requires_creation", True)
                                    feat.setdefault("depends_on", "")
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
            st.caption("Deselecting a parent feature will auto-deselect its dependents.")

            display_df = pd.DataFrame(st.session_state.suggested_features)
            col_order = ["selected", "name", "description", "entity", "source_table",
                         "columns", "transformation", "dtype",
                         "depends_on", "requires_creation"]
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
                    "depends_on": st.column_config.TextColumn("Depends On", disabled=True),
                    "requires_creation": st.column_config.CheckboxColumn("Needs Creation", disabled=True),
                },
                hide_index=True,
                use_container_width=True,
                key="feature_editor",
            )

            selected_names = set(edited[edited["selected"] == True]["name"])
            selected_names, dep_warnings = enforce_dependency_selection(
                st.session_state.suggested_features, selected_names,
            )
            for warn in dep_warnings:
                st.warning(warn)

            selected = edited[edited["name"].isin(selected_names)]
            total = len(edited)
            st.caption(f"{len(selected)} of {total} features selected")

            has_deps = any(
                f.get("depends_on", "").strip()
                for f in st.session_state.suggested_features
            )
            if has_deps:
                with st.expander("Dependency Graph", expanded=False):
                    try:
                        import graphviz
                        dot = graphviz.Digraph(graph_attr={"rankdir": "LR", "size": "8,4"})
                        for feat in st.session_state.suggested_features:
                            style = "filled" if feat["name"] in selected_names else "dashed"
                            fill = "#d4edda" if feat["name"] in selected_names else "#f8d7da"
                            dot.node(feat["name"], style=style, fillcolor=fill)
                            deps = feat.get("depends_on", "")
                            if isinstance(deps, str):
                                deps = [d.strip() for d in deps.split(",") if d.strip()]
                            for dep in deps:
                                dot.edge(dep, feat["name"])
                        st.graphviz_chart(dot)
                    except ImportError:
                        st.info("Install `graphviz` to see the dependency graph.")

            st.divider()

            repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feature_repo")

            if st.button(
                "Generate & Apply Features",
                type="primary",
                disabled=len(selected) == 0,
            ):
                sel_features = selected.drop(columns=["selected"]).to_dict("records")
                for feat in sel_features:
                    if isinstance(feat.get("columns"), str):
                        feat["columns"] = [c.strip() for c in feat["columns"].split(",")]
                    if isinstance(feat.get("depends_on"), str):
                        feat["depends_on"] = [d.strip() for d in feat["depends_on"].split(",") if d.strip()]

                progress = st.empty()
                log_expander = st.expander("Pipeline Logs", expanded=False)
                log_container = log_expander.container()
                st.session_state.feature_repo_generated = False
                st.session_state.feast_applied = False
                st.session_state.feast_materialized = False
                st.session_state.historical_features_df = None
                st.session_state.transformation_commands = {}
                st.session_state.transformation_results = {}

                source_types = st.session_state.get("table_source_types", {})

                # Filter out MongoDB-sourced features (and dependents) from the pipeline
                mongo_features = {f["name"] for f in sel_features
                                  if source_types.get(f["source_table"]) == "mongodb"}
                if mongo_features:
                    # Cascade: also exclude features that depend on a MongoDB feature
                    graph = build_dependency_graph(sel_features)
                    for mf in list(mongo_features):
                        mongo_features.update(get_all_descendants(graph, mf))
                    excluded = [f for f in sel_features if f["name"] in mongo_features]
                    sel_features = [f for f in sel_features if f["name"] not in mongo_features]
                    # Strip dropped mongo deps from remaining features' depends_on lists
                    for feat in sel_features:
                        feat["depends_on"] = [d for d in feat.get("depends_on", [])
                                              if d not in mongo_features]

                def _log(msg, level="info"):
                    icon = {"info": "ℹ️", "ok": "✅", "warn": "⚠️", "err": "❌"}.get(level, "")
                    log_container.markdown(f"`{datetime.utcnow().strftime('%H:%M:%S')}` {icon} {msg}")

                if mongo_features:
                    _log(f"Excluded **{len(mongo_features)}** MongoDB-sourced features from pipeline: {', '.join(f'`{n}`' for n in sorted(mongo_features))}", "warn")
                    _log(f"**{len(sel_features)}** PostgreSQL features remain.")

                # ── Step 1: Sort by dependency order ──
                progress.info("Step 1/6 — Sorting features by dependency order...")
                sorted_features = topological_sort_features(sel_features)
                _log(f"Sorted **{len(sorted_features)}** features in dependency order.")
                for i, f in enumerate(sorted_features):
                    deps = f.get("depends_on", [])
                    dep_str = f" ← depends on: {', '.join(deps)}" if deps else ""
                    needs = " *(needs creation)*" if f.get("requires_creation") else ""
                    _log(f"  {i+1}. `{f['name']}` from `{f['source_table']}`{dep_str}{needs}")

                # ── Step 2: Generate + execute transformations ──
                creation_features = [f for f in sorted_features if f.get("requires_creation")]
                pg_conn_str = next(
                    (c["conn"] for c in st.session_state.connections
                     if c["type"] == "PostgreSQL" and c["conn"].strip()),
                    None,
                )
                if creation_features and pg_conn_str:
                    progress.info(f"Step 2/5 — Building & executing {len(creation_features)} transformations...")
                    _log(f"**Building & executing** {len(creation_features)} computed features (with retry on error)...")

                    pg_context = ""
                    pg_conn_cfg = next(
                        (c for c in st.session_state.connections
                         if c["type"] == "PostgreSQL" and c["conn"].strip()),
                        None,
                    )
                    if pg_conn_cfg:
                        _log("Fetching DB context (DDL, foreign keys, date ranges, sample rows)...")
                        pg_context = _get_pg_context(pg_conn_cfg["conn"])
                        _log(f"DB context: {len(pg_context)} chars.", "ok")

                    results = {}
                    prior_sqls = []
                    for feat in creation_features:
                        _log(f"  `{feat['name']}` — `{feat.get('transformation', 'N/A')[:80]}`")

                        def _on_attempt(attempt, sql, _name=feat["name"]):
                            if attempt == 0:
                                log_container.code(sql, language="sql")
                            else:
                                _log(f"    Retry {attempt}/3 for `{_name}`...", "warn")
                                log_container.code(sql, language="sql")

                        try:
                            ok, sql, err = generate_and_execute_sql(
                                feat, pg_conn_str,
                                llm_url, llm_key, transform_model,
                                pg_context=pg_context,
                                prior_sqls=prior_sqls,
                                on_attempt=_on_attempt,
                                hoop_config=(
                                    {"gateway_url": hoop_url, "api_key": hoop_api_key, "connection": hoop_pg_conn}
                                    if hoop_enabled else None
                                ),
                            )
                            st.session_state.transformation_commands[feat["name"]] = {"type": "sql", "command": sql}
                            if ok:
                                prior_sqls.append({"name": feat["name"], "sql": sql})
                                results[feat["name"]] = (True, "OK")
                                _log(f"    Executed successfully.", "ok")
                            else:
                                results[feat["name"]] = (False, err)
                                _log(f"    Failed after 3 attempts: {err[:200]}", "err")
                        except Exception as e:
                            results[feat["name"]] = (False, str(e))
                            _log(f"    Error: {e}", "err")

                    st.session_state.transformation_results = results
                    ok_count = sum(1 for v in results.values() if v[0])
                    _log(f"Transformations: **{ok_count}/{len(results)}** succeeded.", "ok" if ok_count == len(results) else "warn")
                elif creation_features:
                    _log("No PostgreSQL connection — cannot execute transformations.", "err")
                else:
                    _log("No features require creation — skipping transformations.")

                # ── Step 3: Generate Feast repo (skip failed transformations) ──
                progress.info("Step 3/5 — Generating feature repo...")
                failed_transforms = {
                    name for name, (ok, _) in st.session_state.get("transformation_results", {}).items()
                    if not ok
                }
                graph = build_dependency_graph(sorted_features)
                for failed in list(failed_transforms):
                    failed_transforms.update(get_all_descendants(graph, failed))
                viable_features = [
                    f for f in sorted_features
                    if f["name"] not in failed_transforms
                ]
                if failed_transforms:
                    _log(f"Skipping **{len(failed_transforms)}** features with failed transformations: {', '.join(f'`{n}`' for n in failed_transforms)}", "warn")
                _log(f"**Generating Feast repo** for {len(viable_features)} features...")
                try:
                    created, view_map = generate_feature_repo(
                        viable_features,
                        st.session_state.connections,
                        repo_dir,
                        table_source_types=source_types,
                    )
                    st.session_state.feature_repo_generated = True
                    st.session_state.selected_features = viable_features
                    st.session_state.view_map = view_map
                    _log(f"Created: {', '.join(f'`{c}`' for c in created)}", "ok")
                except Exception as e:
                    _log(f"Generation failed: {e}", "err")
                    st.error(f"Generation failed: {e}")

                # ── Step 4: Feast apply + materialize ──
                if st.session_state.feature_repo_generated:
                    progress.info("Step 4/5 — Running feast apply & materialize...")
                    _log("**Running feast apply**...")
                    try:
                        success, output = run_feast_apply(repo_dir)
                        st.session_state.feast_applied = success
                        if success:
                            _log("feast apply succeeded.", "ok")
                        else:
                            _log(f"feast apply failed:\n```\n{output[:500]}\n```", "err")
                    except Exception as e:
                        _log(f"feast apply error: {e}", "err")

                if st.session_state.get("feast_applied"):
                    _log("**Running feast materialize**...")
                    try:
                        success, output = run_feast_materialize(repo_dir, pg_conn_str=pg_conn_str)
                        st.session_state.feast_materialized = success
                        if success:
                            _log(f"{output}", "ok")
                        else:
                            _log(f"Materialization issue: {output[:300]}", "warn")
                    except Exception as e:
                        _log(f"Materialization skipped: {e}", "warn")

                # ── Step 5: Get historical features ──
                if st.session_state.get("feast_applied"):
                    progress.info("Step 5/5 — Retrieving historical features...")
                    _log("**Retrieving historical features**...")
                    if pg_conn_str:
                        try:
                            vm = st.session_state.get("view_map", {})
                            result = run_get_historical_features(
                                repo_dir, viable_features, pg_conn_str,
                                view_map=vm,
                            )
                            if isinstance(result, pd.DataFrame):
                                st.session_state.historical_features_df = result
                                _log(f"Retrieved **{len(result)} rows** x **{len(result.columns)} columns**.", "ok")
                            else:
                                _log(f"No data: {result}", "warn")
                        except Exception as e:
                            _log(f"Retrieval error: {e}", "warn")

                if st.session_state.get("feast_applied"):
                    progress.success(
                        "Feature pipeline complete — transformations executed, "
                        "repo generated, applied"
                        + (", materialized" if st.session_state.get("feast_materialized") else "")
                        + ", and historical features retrieved."
                    )
                    _log("**Pipeline complete.**", "ok")

            if st.session_state.get("transformation_commands"):
                with st.expander("View generated transformations"):
                    for feat_name, cmd in st.session_state.transformation_commands.items():
                        result = st.session_state.get("transformation_results", {}).get(feat_name)
                        status = "OK" if result and result[0] else "FAILED" if result else "PENDING"
                        st.markdown(f"**{feat_name}** — {status}")
                        st.code(cmd["command"], language="sql")
                        if result and not result[0]:
                            st.error(result[1])

            if st.session_state.get("feature_repo_generated"):
                with st.expander("View generated feature_store.yaml"):
                    yaml_path = os.path.join(repo_dir, "feature_store.yaml")
                    if os.path.exists(yaml_path):
                        with open(yaml_path) as fh:
                            st.code(fh.read(), language="yaml")

                with st.expander("View generated features.py"):
                    py_path = os.path.join(repo_dir, "features.py")
                    if os.path.exists(py_path):
                        with open(py_path) as fh:
                            st.code(fh.read(), language="python")

            if st.session_state.get("feature_repo_generated"):
                st.divider()
                if st.button("Re-run Apply & Materialize", help="Re-run feast apply, materialize, and historical features without regenerating transformations."):
                    rerun_log_expander = st.expander("Apply Logs", expanded=False)
                    rerun_log = rerun_log_expander.container()
                    rerun_progress = st.empty()

                    def _rlog(msg, level="info"):
                        icon = {"info": "ℹ️", "ok": "✅", "warn": "⚠️", "err": "❌"}.get(level, "")
                        rerun_log.markdown(f"`{datetime.utcnow().strftime('%H:%M:%S')}` {icon} {msg}")

                    rerun_progress.info("Running feast apply...")
                    _rlog("**Running feast apply**...")
                    try:
                        success, output = run_feast_apply(repo_dir)
                        st.session_state.feast_applied = success
                        if success:
                            _rlog("feast apply succeeded.", "ok")
                        else:
                            _rlog(f"feast apply failed:\n```\n{output[:500]}\n```", "err")
                    except Exception as e:
                        _rlog(f"feast apply error: {e}", "err")

                    if st.session_state.get("feast_applied"):
                        rerun_progress.info("Running feast materialize...")
                        _rlog("**Running feast materialize**...")
                        try:
                            pg_cs = next((c["conn"] for c in st.session_state.connections if c["type"] == "PostgreSQL" and c["conn"].strip()), None)
                            success, output = run_feast_materialize(repo_dir, pg_conn_str=pg_cs)
                            st.session_state.feast_materialized = success
                            if success:
                                _rlog(f"{output}", "ok")
                            else:
                                _rlog(f"Materialization issue: {output[:300]}", "warn")
                        except Exception as e:
                            _rlog(f"Materialization skipped: {e}", "warn")

                    if st.session_state.get("feast_applied"):
                        rerun_progress.info("Retrieving historical features...")
                        _rlog("**Retrieving historical features**...")
                        pg_conn_cfg = next(
                            (c for c in st.session_state.connections
                             if c["type"] == "PostgreSQL" and c["conn"].strip()),
                            None,
                        )
                        if pg_conn_cfg:
                            try:
                                sel = st.session_state.get("selected_features", [])
                                vm = st.session_state.get("view_map", {})
                                result = run_get_historical_features(repo_dir, sel, pg_conn_cfg["conn"], view_map=vm)
                                if isinstance(result, pd.DataFrame):
                                    st.session_state.historical_features_df = result
                                    _rlog(f"Retrieved **{len(result)} rows** x **{len(result.columns)} columns**.", "ok")
                                else:
                                    _rlog(f"No data: {result}", "warn")
                            except Exception as e:
                                _rlog(f"Retrieval error: {e}", "warn")

                    if st.session_state.get("feast_applied"):
                        rerun_progress.success("Apply + materialize + retrieval complete.")
                        _rlog("**Done.**", "ok")

            if st.session_state.get("historical_features_df") is not None:
                st.divider()
                st.subheader("Historical Features")
                st.dataframe(
                    st.session_state.historical_features_df,
                    use_container_width=True,
                    hide_index=True,
                )
                st.caption(
                    f"{len(st.session_state.historical_features_df)} rows x "
                    f"{len(st.session_state.historical_features_df.columns)} columns"
                )

            if st.session_state.get("feast_applied"):
                st.divider()
                with st.expander("SDK Code Snippets"):
                    snippets = generate_sdk_snippets(
                        repo_dir, st.session_state.get("selected_features", []),
                        view_map=st.session_state.get("view_map", {}),
                    )
                    st.code(snippets, language="python")
