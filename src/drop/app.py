import re
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from drop import __version__
from drop.config import get_settings
from drop.db import (
    VALID_STATUSES,
    delete_drop,
    get_conn,
    init_db,
    insert_drop,
    list_drops,
    update_drop,
)

TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(get_settings().database_url)
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}


def _require_auth(authorization: str | None, token: str) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="unauthorized")
    scheme, _, t = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(t, token):
        raise HTTPException(status_code=401, detail="unauthorized")


class DropUpdate(BaseModel):
    status: str | None = None
    body: str | None = None


@app.get("/drops")
def drops_list(
    topic: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    authorization: str | None = Header(default=None),
):
    settings = get_settings()
    _require_auth(authorization, settings.token)
    if status and status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400, detail="invalid status: use 'inbound' or 'archived'"
        )
    with get_conn(settings.database_url) as conn:
        return list_drops(conn, topic=topic, status=status, limit=limit)


@app.patch("/drops/{id_}")
def drops_update(
    id_: str,
    payload: DropUpdate,
    authorization: str | None = Header(default=None),
):
    settings = get_settings()
    _require_auth(authorization, settings.token)
    if payload.status is not None and payload.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400, detail="invalid status: use 'inbound' or 'archived'"
        )
    if payload.status is None and payload.body is None:
        raise HTTPException(status_code=400, detail="provide at least status or body")
    with get_conn(settings.database_url) as conn:
        row = update_drop(conn, id_, status=payload.status, body=payload.body)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return row


@app.delete("/drops/{id_}", status_code=204)
def drops_delete(
    id_: str,
    authorization: str | None = Header(default=None),
):
    settings = get_settings()
    _require_auth(authorization, settings.token)
    with get_conn(settings.database_url) as conn:
        found = delete_drop(conn, id_)
    if not found:
        raise HTTPException(status_code=404, detail="not found")


@app.post("/drop/{topic}", status_code=201)
async def drop_endpoint(
    topic: str, request: Request, authorization: str | None = Header(default=None)
):
    settings = get_settings()
    _require_auth(authorization, settings.token)

    if not TOPIC_RE.match(topic):
        raise HTTPException(status_code=400, detail="invalid topic")

    body_bytes = await request.body()
    if len(body_bytes) > settings.max_body_bytes:
        raise HTTPException(status_code=413, detail="body too large")

    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="invalid utf-8 body")

    with get_conn(settings.database_url) as conn:
        return insert_drop(conn, topic, body)
