import json
import sys
from openai import OpenAI

OGX_BASE_URL = "http://localhost:11434/v1"
OGX_API_KEY = "ollama"
MODEL = "llama3.1:8b"

SYSTEM_PROMPT = """You are a senior data analyst. Given a database schema in JSON format, perform a Basic Exploratory Data Analysis (EDA). Your analysis should cover:

1. Overview of each database and its tables/collections
2. Column/field data type assessment — flag any that look miscategorized
3. Identify potential primary keys, foreign keys, and join paths between tables
4. Flag columns likely containing PII (names, emails, phone numbers, etc.)
5. Suggest data quality checks (nulls, duplicates, outliers)
6. Recommend basic statistical summaries per column type (mean/median for numeric, cardinality for categorical, range for dates)
7. Suggest useful visualizations for this data

Be specific to the actual table and column names in the schema."""


def run_eda(schema_path: str):
    with open(schema_path) as f:
        schema = json.load(f)

    schema_text = json.dumps(schema, indent=2)

    client = OpenAI(base_url=OGX_BASE_URL, api_key=OGX_API_KEY)
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Here is the database schema:\n\n{schema_text}\n\nPerform a basic EDA analysis."},
        ],
    )

    result = response.choices[0].message.content
    print(result)

    out_file = schema_path.replace(".json", "_eda.txt")
    with open(out_file, "w") as f:
        f.write(result)
    print(f"\nSaved EDA to {out_file}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "schema_output.json"
    run_eda(path)
