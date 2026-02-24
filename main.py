import sqlite3
from fastapi import FastAPI, HTTPException
from database import DB_PATH, get_conn, init_db

app = FastAPI()

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def root():
    return {"message": "Server running"}

@app.get("/debug/db")
def debug_db():
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail="app.db was not created")

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = [r["name"] for r in rows]
    finally:
        conn.close()

    required = {"orders", "ledger", "idempotency_records"}
    missing = sorted(list(required - set(tables)))
    if missing:
        raise HTTPException(status_code=500, detail={"missing_tables": missing, "tables_found": tables})

    return {"db_path": str(DB_PATH), "tables": tables, "ok": True}