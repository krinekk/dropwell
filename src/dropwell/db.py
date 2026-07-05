import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2


def init_db(url: str) -> None:
    schema = Path(__file__).parent / "schema.sql"
    statements = [s.strip() for s in schema.read_text().split(";") if s.strip()]
    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)
    conn.close()


@contextmanager
def get_conn(url: str):
    conn = psycopg2.connect(url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


VALID_STATUSES = {"inbound", "archived"}


def _row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0], "topic": row[1], "body": row[2],
        "received_at": row[3], "updated_at": row[4], "status": row[5],
    }


def list_drops(
    conn,
    topic: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    query = 'SELECT id, topic, body, received_at, updated_at, status FROM "drop"'
    params: list = []
    conditions = []
    if topic:
        conditions.append("topic = %s")
        params.append(topic)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY received_at DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def update_drop(
    conn,
    id_: str,
    status: str | None = None,
    body: str | None = None,
) -> dict | None:
    if status is None and body is None:
        return None
    now = datetime.now(timezone.utc).isoformat()
    fields, params = ["updated_at = %s"], [now]
    if status is not None:
        fields.append("status = %s")
        params.append(status)
    if body is not None:
        fields.append("body = %s")
        params.append(body)
    params.append(id_)
    with conn.cursor() as cur:
        cur.execute(
            f'UPDATE "drop" SET {", ".join(fields)} WHERE id = %s '
            "RETURNING id, topic, body, received_at, updated_at, status",
            params,
        )
        row = cur.fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def delete_drop(conn, id_: str) -> bool:
    with conn.cursor() as cur:
        cur.execute('DELETE FROM "drop" WHERE id = %s', (id_,))
        return cur.rowcount == 1


def insert_drop(conn, topic: str, body: str) -> dict:
    id_ = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            'INSERT INTO "drop" (id, topic, body, received_at, updated_at) '
            "VALUES (%s, %s, %s, %s, %s)",
            (id_, topic, body, now, now),
        )
    return {"id": id_, "topic": topic, "received_at": now, "updated_at": now}
