import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from database import DB_PATH, get_conn, init_db

app = FastAPI()

class OrderCreate(BaseModel):
    customer_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

@app.on_event("startup")
def startup():
    init_db()

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    request.state.request_id = request_id
    start = time.time()
    try:
        response: Response = await call_next(request)
    except Exception:
        raise
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Response-Time-Ms"] = str(int((time.time() - start) * 1000))
    return response

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

@app.post("/orders", status_code=201)
def create_order(
    body: OrderCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    fail_after_commit: bool | None = Header(default=False, alias="X-Debug-Fail-After-Commit"),
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Missing Idempotency-Key")

    payload = body.model_dump()
    req_hash = _fingerprint(payload)

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")

        row = conn.execute(
            "SELECT request_hash, status_code, response_body FROM idempotency_records WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

        if row:
            if row["request_hash"] != req_hash:
                conn.rollback()
                raise HTTPException(status_code=409, detail="Idempotency-Key reuse with different payload")
            stored_status = int(row["status_code"])
            stored_body = json.loads(row["response_body"])
            return Response(content=json.dumps(stored_body), media_type="application/json", status_code=stored_status)

        order_id = uuid4().hex
        ledger_id = uuid4().hex
        now = _utc_now()
        amount_cents = int(payload["quantity"]) * 100

        conn.execute(
            "INSERT INTO orders(order_id, customer_id, item_id, quantity, created_at) VALUES(?,?,?,?,?)",
            (order_id, payload["customer_id"], payload["item_id"], payload["quantity"], now),
        )
        conn.execute(
            "INSERT INTO ledger(ledger_id, order_id, customer_id, amount_cents, created_at) VALUES(?,?,?,?,?)",
            (ledger_id, order_id, payload["customer_id"], amount_cents, now),
        )

        response_body = {"order_id": order_id, "status": "created"}
        conn.execute(
            "INSERT INTO idempotency_records(idempotency_key, request_hash, status_code, response_body, created_at) VALUES(?,?,?,?,?)",
            (idempotency_key, req_hash, 201, json.dumps(response_body), now),
        )

        conn.commit()

        if bool(fail_after_commit):
            raise HTTPException(status_code=500, detail="Simulated post-commit failure")

        return response_body
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Internal server error") from e
    finally:
        conn.close()

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT order_id, customer_id, item_id, quantity, created_at FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    return dict(row)