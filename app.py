"""
Financial Cloud Platform - simple base version
----------------------------------------------
A minimal REST API for cloud-hosted financial services:
  - Create accounts
  - Deposit / withdraw / transfer money
  - View balances and transaction history

Stack: Python 3.9+, Flask, SQLite (swap for PostgreSQL/MySQL in the cloud).

Run:
    pip install flask
    python app.py
"""

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Flask, g, jsonify, request

DB_PATH = os.environ.get("FCP_DB_PATH", "fcp.db")
API_KEY = os.environ.get("FCP_API_KEY", "dev-secret-key")  # set a real key in production

app = Flask(__name__)


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id         TEXT PRIMARY KEY,
                owner      TEXT NOT NULL,
                currency   TEXT NOT NULL DEFAULT 'USD',
                balance    TEXT NOT NULL DEFAULT '0.00',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id          TEXT PRIMARY KEY,
                account_id  TEXT NOT NULL REFERENCES accounts(id),
                type        TEXT NOT NULL,          -- deposit | withdrawal | transfer_in | transfer_out
                amount      TEXT NOT NULL,
                counterparty TEXT,
                created_at  TEXT NOT NULL
            );
            """
        )
        db.commit()


# ----------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc).isoformat()


def parse_amount(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid amount")
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")
    return amount


def error(message, status=400):
    return jsonify({"error": message}), status


@app.before_request
def require_api_key():
    if request.path == "/health":
        return None
    if request.headers.get("X-API-Key") != API_KEY:
        return error("Unauthorized", 401)


def fetch_account(account_id):
    return get_db().execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


def record(db, account_id, tx_type, amount, counterparty=None):
    db.execute(
        "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), account_id, tx_type, str(amount), counterparty, now()),
    )


def set_balance(db, account_id, new_balance):
    db.execute("UPDATE accounts SET balance = ? WHERE id = ?", (str(new_balance), account_id))


# ------------------------------------------------------------------ routes
@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/accounts")
def create_account():
    data = request.get_json(silent=True) or {}
    owner = (data.get("owner") or "").strip()
    if not owner:
        return error("'owner' is required")
    currency = (data.get("currency") or "USD").upper()
    account_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        "INSERT INTO accounts (id, owner, currency, balance, created_at) VALUES (?, ?, ?, '0.00', ?)",
        (account_id, owner, currency, now()),
    )
    db.commit()
    return jsonify({"id": account_id, "owner": owner, "currency": currency, "balance": "0.00"}), 201


@app.get("/accounts/<account_id>")
def get_account(account_id):
    acc = fetch_account(account_id)
    if not acc:
        return error("Account not found", 404)
    return jsonify(dict(acc))


@app.post("/accounts/<account_id>/deposit")
def deposit(account_id):
    acc = fetch_account(account_id)
    if not acc:
        return error("Account not found", 404)
    try:
        amount = parse_amount((request.get_json(silent=True) or {}).get("amount"))
    except ValueError as e:
        return error(str(e))
    db = get_db()
    new_balance = Decimal(acc["balance"]) + amount
    set_balance(db, account_id, new_balance)
    record(db, account_id, "deposit", amount)
    db.commit()
    return jsonify({"id": account_id, "balance": str(new_balance)})


@app.post("/accounts/<account_id>/withdraw")
def withdraw(account_id):
    acc = fetch_account(account_id)
    if not acc:
        return error("Account not found", 404)
    try:
        amount = parse_amount((request.get_json(silent=True) or {}).get("amount"))
    except ValueError as e:
        return error(str(e))
    balance = Decimal(acc["balance"])
    if amount > balance:
        return error("Insufficient funds", 422)
    db = get_db()
    new_balance = balance - amount
    set_balance(db, account_id, new_balance)
    record(db, account_id, "withdrawal", amount)
    db.commit()
    return jsonify({"id": account_id, "balance": str(new_balance)})


@app.post("/transfers")
def transfer():
    data = request.get_json(silent=True) or {}
    src, dst = data.get("from"), data.get("to")
    if not src or not dst or src == dst:
        return error("Provide different 'from' and 'to' account ids")
    try:
        amount = parse_amount(data.get("amount"))
    except ValueError as e:
        return error(str(e))

    a, b = fetch_account(src), fetch_account(dst)
    if not a or not b:
        return error("Account not found", 404)
    if a["currency"] != b["currency"]:
        return error("Cross-currency transfers are not supported in the base version", 422)
    if amount > Decimal(a["balance"]):
        return error("Insufficient funds", 422)

    db = get_db()
    try:
        set_balance(db, src, Decimal(a["balance"]) - amount)
        set_balance(db, dst, Decimal(b["balance"]) + amount)
        record(db, src, "transfer_out", amount, dst)
        record(db, dst, "transfer_in", amount, src)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"status": "completed", "from": src, "to": dst, "amount": str(amount)})


@app.get("/accounts/<account_id>/transactions")
def transactions(account_id):
    if not fetch_account(account_id):
        return error("Account not found", 404)
    rows = get_db().execute(
        "SELECT * FROM transactions WHERE account_id = ? ORDER BY created_at DESC", (account_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# -------------------------------------------------------------------- main
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
