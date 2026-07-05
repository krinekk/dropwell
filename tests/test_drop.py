import os

import psycopg2
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from dropwell.config import get_settings

load_dotenv()

TEST_DB_URL = os.getenv(
    "TEST_DROPWELL_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/drop_test",
)


@pytest.fixture(scope="session", autouse=True)
def create_test_tables():
    from dropwell.db import init_db

    init_db(TEST_DB_URL)


@pytest.fixture(autouse=True)
def clean_table():
    yield
    conn = psycopg2.connect(TEST_DB_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute('TRUNCATE TABLE "drop"')
    conn.close()


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("DROPWELL_TOKEN", "test-token")
    monkeypatch.setenv("DROPWELL_DATABASE_URL", TEST_DB_URL)
    get_settings.cache_clear()

    from dropwell.app import app

    with TestClient(app) as client:
        yield client

    get_settings.cache_clear()


TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_settings_parses_cors_origins():
    from dropwell.config import Settings

    settings = Settings(
        token="test-token",
        database_url="postgresql://postgres:postgres@localhost:5432/drop_test",
        cors_origins="http://localhost:3000, https://drop.example.com, ",
    )

    assert settings.cors_origin_list == [
        "http://localhost:3000",
        "https://drop.example.com",
    ]


def test_health_ok(app_client):
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_drop_requires_auth(app_client):
    r = app_client.post("/drop/note", content="hello")
    assert r.status_code == 401


def test_drop_rejects_wrong_token(app_client):
    r = app_client.post(
        "/drop/note", content="hello", headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code == 401


@pytest.mark.parametrize("topic", ["UPPER", "with space", "with/slash", ""])
def test_drop_rejects_invalid_topic(app_client, topic):
    r = app_client.post(f"/drop/{topic}", content="hello", headers=AUTH)
    assert r.status_code in (400, 404, 405)


def test_drop_persists_row(app_client):
    r = app_client.post("/drop/note", content="hello world", headers=AUTH)
    assert r.status_code == 201
    data = r.json()
    assert data["topic"] == "note"
    assert "id" in data
    assert "received_at" in data

    conn = psycopg2.connect(TEST_DB_URL)
    with conn.cursor() as cur:
        cur.execute(
            'SELECT id, topic, body FROM "drop" WHERE id = %s', (data["id"],)
        )
        row = cur.fetchone()
    conn.close()
    assert row is not None
    assert row[1] == "note"
    assert row[2] == "hello world"


def test_drop_returns_unique_ids(app_client):
    r1 = app_client.post("/drop/note", content="a", headers=AUTH)
    r2 = app_client.post("/drop/note", content="b", headers=AUTH)
    assert r1.json()["id"] != r2.json()["id"]


def test_drop_rejects_large_body(app_client):
    r = app_client.post("/drop/note", content=b"x" * 10_485_761, headers=AUTH)
    assert r.status_code == 413


def test_drops_list_requires_auth(app_client):
    r = app_client.get("/drops")
    assert r.status_code == 401


def test_drops_list_empty(app_client):
    r = app_client.get("/drops", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []


def test_drops_list_returns_items(app_client):
    app_client.post("/drop/note", content="first", headers=AUTH)
    app_client.post("/drop/idea", content="second", headers=AUTH)
    r = app_client.get("/drops", headers=AUTH)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert all(
        "id" in i and "topic" in i and "body" in i
        and "received_at" in i and "status" in i
        for i in items
    )
    assert all(i["status"] == "inbound" for i in items)


def test_drops_list_filter_by_topic(app_client):
    app_client.post("/drop/note", content="note1", headers=AUTH)
    app_client.post("/drop/idea", content="idea1", headers=AUTH)
    r = app_client.get("/drops?topic=note", headers=AUTH)
    items = r.json()
    assert len(items) == 1
    assert items[0]["topic"] == "note"


def test_drops_list_limit(app_client):
    for i in range(5):
        app_client.post("/drop/note", content=f"msg{i}", headers=AUTH)
    r = app_client.get("/drops?limit=3", headers=AUTH)
    assert len(r.json()) == 3


def test_drops_list_filter_by_status(app_client):
    r1 = app_client.post("/drop/note", content="a", headers=AUTH)
    id1 = r1.json()["id"]
    app_client.post("/drop/note", content="b", headers=AUTH)
    app_client.patch(f"/drops/{id1}", json={"status": "archived"}, headers=AUTH)
    inbound = app_client.get("/drops?status=inbound", headers=AUTH).json()
    archived = app_client.get("/drops?status=archived", headers=AUTH).json()
    assert len(inbound) == 1 and inbound[0]["status"] == "inbound"
    assert len(archived) == 1 and archived[0]["id"] == id1


def test_drops_patch_status(app_client):
    r = app_client.post("/drop/note", content="hello", headers=AUTH)
    id_ = r.json()["id"]
    r2 = app_client.patch(f"/drops/{id_}", json={"status": "archived"}, headers=AUTH)
    assert r2.status_code == 200
    assert r2.json()["status"] == "archived"


def test_drops_patch_invalid_status(app_client):
    r = app_client.post("/drop/note", content="hello", headers=AUTH)
    id_ = r.json()["id"]
    r2 = app_client.patch(f"/drops/{id_}", json={"status": "bad"}, headers=AUTH)
    assert r2.status_code == 400


def test_drops_patch_not_found(app_client):
    r = app_client.patch(
        "/drops/nonexistent", json={"status": "archived"}, headers=AUTH
    )
    assert r.status_code == 404


def test_drops_delete(app_client):
    r = app_client.post("/drop/note", content="to delete", headers=AUTH)
    id_ = r.json()["id"]
    r2 = app_client.delete(f"/drops/{id_}", headers=AUTH)
    assert r2.status_code == 204
    items = app_client.get("/drops", headers=AUTH).json()
    assert not any(i["id"] == id_ for i in items)


def test_drops_delete_not_found(app_client):
    r = app_client.delete("/drops/nonexistent", headers=AUTH)
    assert r.status_code == 404


def test_drop_accepts_arbitrary_content_type(app_client):
    for ct in ("application/json", "text/plain", "application/octet-stream"):
        r = app_client.post(
            "/drop/note",
            content='{"key": "value"}',
            headers={**AUTH, "Content-Type": ct},
        )
        assert r.status_code == 201
