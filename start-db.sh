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

echo ""
echo "All services are running."

# --- Seed test data ---
echo ""
echo "Seeding test data..."
uv run python setup_test_data.py
echo "Done. Databases are running and seeded."
