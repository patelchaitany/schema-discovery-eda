#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PG_CONTAINER="banking_postgres"
MONGO_CONTAINER="banking_mongo"

# --- PostgreSQL ---
echo "Starting PostgreSQL..."
if docker ps -q -f name="^${PG_CONTAINER}$" | grep -q .; then
  echo "  Already running."
elif docker ps -aq -f name="^${PG_CONTAINER}$" | grep -q .; then
  docker start "$PG_CONTAINER"
else
  docker run -d \
    --name "$PG_CONTAINER" \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=banking_db \
    -p 5432:5432 \
    postgres:16-alpine
fi

# --- MongoDB ---
echo "Starting MongoDB..."
if docker ps -q -f name="^${MONGO_CONTAINER}$" | grep -q .; then
  echo "  Already running."
elif docker ps -aq -f name="^${MONGO_CONTAINER}$" | grep -q .; then
  docker start "$MONGO_CONTAINER"
else
  docker run -d \
    --name "$MONGO_CONTAINER" \
    -p 27017:27017 \
    mongo:7
fi

# --- Wait for databases ---
echo ""
echo "Waiting for databases to be ready..."

for i in {1..30}; do
  if docker exec "$PG_CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then
    echo "PostgreSQL is ready (localhost:5432)"
    break
  fi
  sleep 1
done

for i in {1..30}; do
  if docker exec "$MONGO_CONTAINER" mongosh --eval "db.runCommand({ping:1})" --quiet >/dev/null 2>&1; then
    echo "MongoDB is ready (localhost:27017)"
    break
  fi
  sleep 1
done

# --- Hoop Gateway (docker-compose) ---
echo ""
echo "Starting Hoop Gateway..."
docker compose -f docker-compose.hoop.yml up -d

echo "Waiting for Hoop Gateway..."
for i in {1..60}; do
  if curl -sf http://localhost:8009/api/healthz >/dev/null 2>&1; then
    echo "Hoop Gateway is ready (localhost:8009)"
    break
  fi
  sleep 2
done

# --- Hoop: Register admin user + create pg-banking connection ---
echo ""
echo "Configuring Hoop..."

HOOP_EMAIL="admin@local.dev"
HOOP_PASSWORD="hoop-admin-123"

# Register admin user (idempotent — returns 200 if already exists)
curl -sf -X POST http://localhost:8009/api/localauth/register \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"admin\",\"email\":\"${HOOP_EMAIL}\",\"password\":\"${HOOP_PASSWORD}\"}" >/dev/null 2>&1 || true

# Login to get JWT
HOOP_TOKEN=$(curl -sD - -X POST http://localhost:8009/api/localauth/login \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"${HOOP_EMAIL}\",\"password\":\"${HOOP_PASSWORD}\"}" 2>/dev/null \
  | grep -i "^Token:" | sed 's/^Token: *//' | tr -d '\r\n')

if [ -z "$HOOP_TOKEN" ]; then
  echo "  WARNING: Could not get Hoop JWT. Hoop connections must be configured manually."
else
  echo "  Hoop JWT obtained."

  # Get the agent ID
  AGENT_ID=$(curl -s http://localhost:8009/api/agents \
    -H "Authorization: Bearer ${HOOP_TOKEN}" 2>/dev/null \
    | python3 -c "import json,sys; a=json.load(sys.stdin); print(a[0]['id'] if a else '')" 2>/dev/null)

  if [ -n "$AGENT_ID" ]; then
    # Check if pg-banking connection already exists
    EXISTING=$(curl -s http://localhost:8009/api/connections \
      -H "Authorization: Bearer ${HOOP_TOKEN}" 2>/dev/null \
      | python3 -c "import json,sys; print(any(c['name']=='pg-banking' for c in json.load(sys.stdin)))" 2>/dev/null)

    if [ "$EXISTING" = "True" ]; then
      echo "  pg-banking connection already exists."
    else
      curl -sf -X POST http://localhost:8009/api/connections \
        -H "Authorization: Bearer ${HOOP_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{
          \"name\": \"pg-banking\",
          \"type\": \"custom\",
          \"agent_id\": \"${AGENT_ID}\",
          \"access_mode_runbooks\": \"enabled\",
          \"access_mode_exec\": \"enabled\",
          \"access_mode_connect\": \"enabled\",
          \"access_schema\": \"enabled\",
          \"command\": [\"psql\", \"-v\", \"ON_ERROR_STOP=1\", \"-A\", \"-F\\t\", \"-P\", \"pager=off\", \"-h\", \"host.docker.internal\", \"-p\", \"5432\", \"-U\", \"postgres\", \"-d\", \"banking_db\"],
          \"secret\": {
            \"envvar:PGPASSWORD\": \"$(echo -n postgres | base64)\",
            \"envvar:PGSSLMODE\": \"$(echo -n disable | base64)\"
          }
        }" >/dev/null 2>&1 && echo "  pg-banking connection created." || echo "  WARNING: Failed to create pg-banking connection."
    fi
  fi

  echo ""
  echo "  Hoop API Key (paste into the app sidebar):"
  echo "  ${HOOP_TOKEN}"
fi

echo ""
echo "All services are running."

# --- Seed test data ---
echo ""
echo "Seeding test data..."
uv run python setup_test_data.py
echo "Done. Databases are running and seeded."
