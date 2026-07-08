import random
import datetime
import psycopg2
from pymongo import MongoClient

PG_CONN = "host=localhost port=5432 dbname=banking_db user=postgres password=postgres"
MONGO_URI = "mongodb://localhost:27017"
MONGO_DB = "banking_mongo"

FIRST_NAMES = ["Amit", "Priya", "Rahul", "Sneha", "Vikram", "Anjali", "Rohan", "Neha", "Arjun", "Kavita"]
LAST_NAMES = ["Sharma", "Patel", "Singh", "Kumar", "Gupta", "Reddy", "Joshi", "Verma", "Nair", "Das"]
CITIES = ["Mumbai", "Delhi", "Bangalore", "Chennai", "Hyderabad", "Pune", "Kolkata", "Jaipur"]
ACCOUNT_TYPES = ["savings", "current", "fixed_deposit"]
TXN_TYPES = ["credit", "debit", "transfer"]
LOAN_STATUSES = ["active", "closed", "defaulted"]
DEVICES = ["mobile", "desktop", "tablet", "ATM"]

NUM_CUSTOMERS = 20
NUM_TRANSACTIONS = 200
NUM_FRAUD_ALERTS = 30
NUM_AUDIT_ENTRIES = 60


def _now():
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def random_date_relative(max_days_ago=730):
    """Random date between max_days_ago and today."""
    return (_now() - datetime.timedelta(days=random.randint(0, max_days_ago))).date()


def random_datetime_relative(max_days_ago=730):
    """Random datetime between max_days_ago and today, with time component."""
    dt = _now() - datetime.timedelta(
        days=random.randint(0, max_days_ago),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )
    return dt.replace(microsecond=0)


def random_recent_datetime(max_days_ago=30):
    """Random datetime within the last N days — ensures recent data exists."""
    dt = _now() - datetime.timedelta(
        days=random.randint(0, max_days_ago),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )
    return dt.replace(microsecond=0)


def build_customer_list():
    """Build a shared customer list used by both PG and MongoDB."""
    customers = []
    for i in range(NUM_CUSTOMERS):
        fn = FIRST_NAMES[i % len(FIRST_NAMES)]
        ln = LAST_NAMES[i % len(LAST_NAMES)]
        customers.append({
            "id": i + 1,
            "first_name": fn,
            "last_name": ln,
            "email": f"{fn.lower()}.{ln.lower()}{i}@email.com",
            "phone": f"+91{random.randint(7000000000, 9999999999)}",
            "date_of_birth": random_date_relative(max_days_ago=365 * 40),
            "city": random.choice(CITIES),
        })
    return customers


def setup_postgres(customers):
    conn = psycopg2.connect(PG_CONN)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS credit_cards, loans, transactions, accounts, customers CASCADE")

    cur.execute("""
        CREATE TABLE customers (
            id SERIAL PRIMARY KEY,
            first_name VARCHAR(50),
            last_name VARCHAR(50),
            email VARCHAR(100),
            phone VARCHAR(15),
            date_of_birth DATE,
            address TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE accounts (
            id SERIAL PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            account_type VARCHAR(20),
            balance NUMERIC(15,2),
            currency VARCHAR(3) DEFAULT 'INR',
            opened_date DATE,
            status VARCHAR(10) DEFAULT 'active'
        )
    """)
    cur.execute("""
        CREATE TABLE transactions (
            id SERIAL PRIMARY KEY,
            account_id INTEGER REFERENCES accounts(id),
            transaction_type VARCHAR(10),
            amount NUMERIC(12,2),
            timestamp TIMESTAMP,
            description TEXT,
            status VARCHAR(10) DEFAULT 'completed'
        )
    """)
    cur.execute("""
        CREATE TABLE loans (
            id SERIAL PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            principal_amount NUMERIC(15,2),
            interest_rate NUMERIC(4,2),
            term_months INTEGER,
            start_date DATE,
            status VARCHAR(15) DEFAULT 'active'
        )
    """)
    cur.execute("""
        CREATE TABLE credit_cards (
            id SERIAL PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            card_number VARCHAR(16),
            credit_limit NUMERIC(12,2),
            current_balance NUMERIC(12,2),
            expiry_date DATE,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)

    # --- Customers ---
    for c in customers:
        cur.execute(
            "INSERT INTO customers (first_name, last_name, email, phone, date_of_birth, address) VALUES (%s,%s,%s,%s,%s,%s)",
            (c["first_name"], c["last_name"], c["email"], c["phone"],
             c["date_of_birth"],
             f"{random.randint(1, 500)} {c['city']} Road, {c['city']}")
        )

    # --- Accounts (1-2 per customer) ---
    for c in customers:
        for _ in range(random.randint(1, 2)):
            cur.execute(
                "INSERT INTO accounts (customer_id, account_type, balance, opened_date) VALUES (%s,%s,%s,%s)",
                (c["id"], random.choice(ACCOUNT_TYPES),
                 round(random.uniform(1000, 500000), 2),
                 random_date_relative())
            )

    # --- Transactions (bulk, with guaranteed recent data) ---
    cur.execute("SELECT id FROM accounts")
    account_ids = [r[0] for r in cur.fetchall()]

    # 70% historical, 30% recent (last 30 days) to ensure time-window features work
    num_historical = int(NUM_TRANSACTIONS * 0.7)
    num_recent = NUM_TRANSACTIONS - num_historical

    for _ in range(num_historical):
        cur.execute(
            "INSERT INTO transactions (account_id, transaction_type, amount, timestamp, description) VALUES (%s,%s,%s,%s,%s)",
            (random.choice(account_ids), random.choice(TXN_TYPES),
             round(random.uniform(100, 50000), 2),
             random_datetime_relative(),
             random.choice(["ATM withdrawal", "UPI transfer", "NEFT", "salary credit", "bill payment"]))
        )

    for _ in range(num_recent):
        cur.execute(
            "INSERT INTO transactions (account_id, transaction_type, amount, timestamp, description) VALUES (%s,%s,%s,%s,%s)",
            (random.choice(account_ids), random.choice(TXN_TYPES),
             round(random.uniform(100, 50000), 2),
             random_recent_datetime(max_days_ago=30),
             random.choice(["ATM withdrawal", "UPI transfer", "NEFT", "salary credit", "bill payment"]))
        )

    # --- Loans ---
    for cid in random.sample([c["id"] for c in customers], 10):
        cur.execute(
            "INSERT INTO loans (customer_id, principal_amount, interest_rate, term_months, start_date, status) VALUES (%s,%s,%s,%s,%s,%s)",
            (cid, round(random.uniform(50000, 2000000), 2),
             round(random.uniform(7.0, 15.0), 2),
             random.choice([12, 24, 36, 60]),
             random_date_relative(),
             random.choice(LOAN_STATUSES))
        )

    # --- Credit cards ---
    for cid in random.sample([c["id"] for c in customers], 12):
        cur.execute(
            "INSERT INTO credit_cards (customer_id, card_number, credit_limit, current_balance, expiry_date) VALUES (%s,%s,%s,%s,%s)",
            (cid, str(random.randint(4000000000000000, 4999999999999999)),
             round(random.uniform(25000, 500000), 2),
             round(random.uniform(0, 100000), 2),
             random_date_relative(max_days_ago=0) + datetime.timedelta(days=random.randint(365, 1825)))
        )

    cur.close()
    conn.close()
    print(f"PostgreSQL: banking_db seeded — {NUM_CUSTOMERS} customers, {NUM_TRANSACTIONS} transactions.")


def setup_mongodb(customers):
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]

    for col in db.list_collection_names():
        db[col].drop()

    # --- Customer profiles (with customer_id matching PG) ---
    profiles = []
    for c in customers:
        profiles.append({
            "customer_id": c["id"],
            "name": f"{c['first_name']} {c['last_name']}",
            "email": c["email"],
            "phone": c["phone"],
            "date_of_birth": c["date_of_birth"].isoformat(),
            "address": {
                "street": f"{random.randint(1, 500)} MG Road",
                "city": c["city"],
                "pincode": str(random.randint(100000, 999999)),
            },
            "kyc_verified": random.choice([True, False]),
            "preferences": {
                "language": random.choice(["en", "hi", "ta"]),
                "notifications": random.choice([True, False]),
            },
            "created_at": random_datetime_relative(),
        })
    db.customer_profiles.insert_many(profiles)

    # --- Transaction logs (with customer_id, proper BSON datetimes) ---
    txn_logs = []
    num_hist = int(NUM_TRANSACTIONS * 0.7)
    num_recent = NUM_TRANSACTIONS - num_hist
    for _ in range(num_hist):
        c = random.choice(customers)
        txn_logs.append({
            "customer_id": c["id"],
            "customer_email": c["email"],
            "type": random.choice(TXN_TYPES),
            "amount": round(random.uniform(100, 50000), 2),
            "currency": "INR",
            "timestamp": random_datetime_relative(),
            "metadata": {
                "device": random.choice(DEVICES),
                "ip": f"192.168.{random.randint(0, 255)}.{random.randint(1, 254)}",
                "location": random.choice(CITIES),
            },
        })
    for _ in range(num_recent):
        c = random.choice(customers)
        txn_logs.append({
            "customer_id": c["id"],
            "customer_email": c["email"],
            "type": random.choice(TXN_TYPES),
            "amount": round(random.uniform(100, 50000), 2),
            "currency": "INR",
            "timestamp": random_recent_datetime(max_days_ago=30),
            "metadata": {
                "device": random.choice(DEVICES),
                "ip": f"192.168.{random.randint(0, 255)}.{random.randint(1, 254)}",
                "location": random.choice(CITIES),
            },
        })
    db.transaction_logs.insert_many(txn_logs)

    # --- Fraud alerts (with customer_id, proper datetimes) ---
    alerts = []
    for _ in range(NUM_FRAUD_ALERTS):
        c = random.choice(customers)
        alerts.append({
            "customer_id": c["id"],
            "customer_email": c["email"],
            "alert_type": random.choice(["suspicious_login", "large_withdrawal", "international_txn", "rapid_transactions"]),
            "risk_score": round(random.uniform(0.1, 1.0), 2),
            "details": {
                "description": "Flagged by automated system",
                "txn_amount": round(random.uniform(5000, 200000), 2),
            },
            "resolved": random.choice([True, False]),
            "created_at": random_datetime_relative(),
        })
    db.fraud_alerts.insert_many(alerts)

    # --- Branch info ---
    branches = []
    for city in CITIES[:5]:
        branches.append({
            "branch_name": f"{city} Main Branch",
            "branch_code": f"BR{random.randint(1000, 9999)}",
            "location": {
                "city": city,
                "latitude": round(random.uniform(12.0, 28.0), 4),
                "longitude": round(random.uniform(72.0, 88.0), 4),
            },
            "services": random.sample(["locker", "forex", "wealth_mgmt", "insurance", "loans"], k=3),
            "operating_hours": {"open": "09:00", "close": "17:00"},
            "is_active": True,
        })
    db.branch_info.insert_many(branches)

    # --- Audit trails (with customer_id, proper datetimes, recent data) ---
    audits = []
    num_hist_audit = int(NUM_AUDIT_ENTRIES * 0.6)
    num_recent_audit = NUM_AUDIT_ENTRIES - num_hist_audit
    for _ in range(num_hist_audit):
        c = random.choice(customers)
        audits.append({
            "customer_id": c["id"],
            "user": c["email"],
            "action": random.choice(["login", "password_change", "funds_transfer", "profile_update", "beneficiary_add"]),
            "timestamp": random_datetime_relative(),
            "details": {
                "user_agent": random.choice(["Chrome/120", "Safari/17", "MobileApp/3.2"]),
                "ip": f"10.0.{random.randint(0, 255)}.{random.randint(1, 254)}",
            },
            "status": random.choice(["success", "failure"]),
        })
    for _ in range(num_recent_audit):
        c = random.choice(customers)
        audits.append({
            "customer_id": c["id"],
            "user": c["email"],
            "action": random.choice(["login", "password_change", "funds_transfer", "profile_update", "beneficiary_add"]),
            "timestamp": random_recent_datetime(max_days_ago=7),
            "details": {
                "user_agent": random.choice(["Chrome/120", "Safari/17", "MobileApp/3.2"]),
                "ip": f"10.0.{random.randint(0, 255)}.{random.randint(1, 254)}",
            },
            "status": random.choice(["success", "failure"]),
        })
    db.audit_trails.insert_many(audits)

    client.close()
    print(f"MongoDB: {MONGO_DB} seeded — {NUM_CUSTOMERS} profiles, {NUM_TRANSACTIONS} txn logs, {NUM_FRAUD_ALERTS} alerts, {NUM_AUDIT_ENTRIES} audits.")


if __name__ == "__main__":
    customers = build_customer_list()
    setup_postgres(customers)
    setup_mongodb(customers)
