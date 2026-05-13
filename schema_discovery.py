import json
import sys
import psycopg2
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime


PG_CONN = "host=localhost port=5432 dbname=banking_db user=postgres password=postgres"
MONGO_URI = "mongodb://localhost:27017"
MONGO_DB = "banking_mongo"

BSON_TYPE_MAP = {
    str: "string",
    int: "int",
    float: "double",
    bool: "bool",
    list: "array",
    dict: "object",
    datetime: "datetime",
    ObjectId: "ObjectId",
    type(None): "null",
}


def discover_postgres(conn_string: str) -> list[dict]:
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
    cur.close()
    conn.close()
    return [{name: cols} for name, cols in tables.items()]


def discover_mongodb(uri: str, db_name: str, sample_size: int = 100) -> list[dict]:
    client = MongoClient(uri)
    db = client[db_name]
    collections = []
    for col_name in sorted(db.list_collection_names()):
        field_types: dict[str, str] = {}
        for doc in db[col_name].find().limit(sample_size):
            for key, value in doc.items():
                if key not in field_types:
                    field_types[key] = BSON_TYPE_MAP.get(type(value), type(value).__name__)
        collections.append({col_name: field_types})
    client.close()
    return collections


def discover_all() -> dict:
    result = {}
    try:
        result["banking_db"] = discover_postgres(PG_CONN)
    except Exception as e:
        print(f"PostgreSQL error: {e}")

    try:
        result[MONGO_DB] = discover_mongodb(MONGO_URI, MONGO_DB)
    except Exception as e:
        print(f"MongoDB error: {e}")

    return result


if __name__ == "__main__":
    schema = discover_all()
    output = json.dumps(schema, indent=2)
    print(output)

    out_file = sys.argv[1] if len(sys.argv) > 1 else "schema_output.json"
    with open(out_file, "w") as f:
        f.write(output)
    print(f"\nSaved to {out_file}")
