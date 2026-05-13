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


def random_date(start_year=2020, end_year=2025):
    start = datetime.date(start_year, 1, 1)
    end = datetime.date(end_year, 12, 31)
    delta = (end - start).days
    return start + datetime.timedelta(days=random.randint(0, delta))


def random_datetime(start_year=2020, end_year=2025):
    d = random_date(start_year, end_year)
    return datetime.datetime(d.year, d.month, d.day, random.randint(0, 23), random.randint(0, 59))


def setup_postgres():
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

    for i in range(20):
        fn = random.choice(FIRST_NAMES)
        ln = random.choice(LAST_NAMES)
        cur.execute(
            "INSERT INTO customers (first_name, last_name, email, phone, date_of_birth, address) VALUES (%s,%s,%s,%s,%s,%s)",
            (fn, ln, f"{fn.lower()}.{ln.lower()}{i}@email.com", f"+91{random.randint(7000000000,9999999999)}",
             random_date(1970, 2000), f"{random.randint(1,500)} {random.choice(CITIES)} Road, {random.choice(CITIES)}")
        )

    for cid in range(1, 21):
        for _ in range(random.randint(1, 2)):
            cur.execute(
                "INSERT INTO accounts (customer_id, account_type, balance, opened_date) VALUES (%s,%s,%s,%s)",
                (cid, random.choice(ACCOUNT_TYPES), round(random.uniform(1000, 500000), 2), random_date())
            )

    cur.execute("SELECT id FROM accounts")
    account_ids = [r[0] for r in cur.fetchall()]
    for _ in range(50):
        cur.execute(
            "INSERT INTO transactions (account_id, transaction_type, amount, timestamp, description) VALUES (%s,%s,%s,%s,%s)",
            (random.choice(account_ids), random.choice(TXN_TYPES), round(random.uniform(100, 50000), 2),
             random_datetime(), random.choice(["ATM withdrawal", "UPI transfer", "NEFT", "salary credit", "bill payment"]))
        )

    for cid in random.sample(range(1, 21), 10):
        cur.execute(
            "INSERT INTO loans (customer_id, principal_amount, interest_rate, term_months, start_date, status) VALUES (%s,%s,%s,%s,%s,%s)",
            (cid, round(random.uniform(50000, 2000000), 2), round(random.uniform(7.0, 15.0), 2),
             random.choice([12, 24, 36, 60]), random_date(), random.choice(LOAN_STATUSES))
        )

    for cid in random.sample(range(1, 21), 12):
        cur.execute(
            "INSERT INTO credit_cards (customer_id, card_number, credit_limit, current_balance, expiry_date) VALUES (%s,%s,%s,%s,%s)",
            (cid, str(random.randint(4000000000000000, 4999999999999999)),
             round(random.uniform(25000, 500000), 2), round(random.uniform(0, 100000), 2), random_date(2025, 2030))
        )

    cur.close()
    conn.close()
    print("PostgreSQL: banking_db seeded with 5 tables.")


def setup_mongodb():
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]

    for col in db.list_collection_names():
        db[col].drop()

    profiles = []
    for i in range(20):
        fn = random.choice(FIRST_NAMES)
        ln = random.choice(LAST_NAMES)
        profiles.append({
            "name": f"{fn} {ln}",
            "email": f"{fn.lower()}.{ln.lower()}{i}@bank.com",
            "phone": f"+91{random.randint(7000000000,9999999999)}",
            "date_of_birth": random_date(1970, 2000).isoformat(),
            "address": {"street": f"{random.randint(1,500)} MG Road", "city": random.choice(CITIES), "pincode": str(random.randint(100000, 999999))},
            "kyc_verified": random.choice([True, False]),
            "preferences": {"language": random.choice(["en", "hi", "ta"]), "notifications": random.choice([True, False])},
            "created_at": random_datetime().isoformat()
        })
    db.customer_profiles.insert_many(profiles)

    txn_logs = []
    for _ in range(50):
        txn_logs.append({
            "customer_email": random.choice(profiles)["email"],
            "type": random.choice(TXN_TYPES),
            "amount": round(random.uniform(100, 50000), 2),
            "currency": "INR",
            "timestamp": random_datetime().isoformat(),
            "metadata": {"device": random.choice(DEVICES), "ip": f"192.168.{random.randint(0,255)}.{random.randint(1,254)}", "location": random.choice(CITIES)}
        })
    db.transaction_logs.insert_many(txn_logs)

    alerts = []
    for _ in range(15):
        alerts.append({
            "customer_email": random.choice(profiles)["email"],
            "alert_type": random.choice(["suspicious_login", "large_withdrawal", "international_txn", "rapid_transactions"]),
            "risk_score": round(random.uniform(0.1, 1.0), 2),
            "details": {"description": "Flagged by automated system", "txn_amount": round(random.uniform(5000, 200000), 2)},
            "resolved": random.choice([True, False]),
            "created_at": random_datetime().isoformat()
        })
    db.fraud_alerts.insert_many(alerts)

    branches = []
    for city in CITIES[:5]:
        branches.append({
            "branch_name": f"{city} Main Branch",
            "branch_code": f"BR{random.randint(1000,9999)}",
            "location": {"city": city, "latitude": round(random.uniform(12.0, 28.0), 4), "longitude": round(random.uniform(72.0, 88.0), 4)},
            "services": random.sample(["locker", "forex", "wealth_mgmt", "insurance", "loans"], k=3),
            "operating_hours": {"open": "09:00", "close": "17:00"},
            "is_active": True
        })
    db.branch_info.insert_many(branches)

    audits = []
    for _ in range(30):
        audits.append({
            "user": random.choice(profiles)["email"],
            "action": random.choice(["login", "password_change", "funds_transfer", "profile_update", "beneficiary_add"]),
            "timestamp": random_datetime().isoformat(),
            "details": {"user_agent": random.choice(["Chrome/120", "Safari/17", "MobileApp/3.2"]), "ip": f"10.0.{random.randint(0,255)}.{random.randint(1,254)}"},
            "status": random.choice(["success", "failure"])
        })
    db.audit_trails.insert_many(audits)

    client.close()
    print(f"MongoDB: {MONGO_DB} seeded with 5 collections.")


if __name__ == "__main__":
    setup_postgres()
    setup_mongodb()
