import pytest

import app as platform


@pytest.fixture
def client(tmp_path):
    platform.app.config.update(DB_PATH=str(tmp_path / "test.db"), TESTING=True)
    platform.init_db()
    return platform.app.test_client()


def make(client, name, **extra):
    body = {"name": name, "domain": "Sales", "owner": "team@corp.com",
            "schema": [{"name": "id", "type": "int", "nullable": False}, {"name": "amount", "type": "float"}],
            "rules": [{"column": "id", "type": "unique"}, {"column": "amount", "type": "range", "min": 0}]}
    body.update(extra)
    return client.post("/api/products", json=body)


def test_create_and_duplicate(client):
    assert make(client, "orders").status_code == 201
    assert make(client, "orders").status_code == 409


def test_quality_and_publish_gate(client):
    pid = make(client, "orders").get_json()["id"]
    assert client.post(f"/api/products/{pid}/status", json={"status": "published"}).status_code == 400
    bad = client.post(f"/api/products/{pid}/validate", json={"rows": [
        {"id": "1", "amount": "5"}, {"id": "1", "amount": "-3"}, {"id": "x", "amount": "2"}]}).get_json()
    assert bad["score"] < 90
    good = client.post(f"/api/products/{pid}/validate", json={"rows": [
        {"id": "1", "amount": "5"}, {"id": "2", "amount": "7"}]}).get_json()
    assert good["score"] == 100.0
    assert client.post(f"/api/products/{pid}/status", json={"status": "published"}).status_code == 200


def test_csv_validation(client):
    pid = make(client, "orders").get_json()["id"]
    r = client.post(f"/api/products/{pid}/validate", data="id,amount\n1,5\n2,6\n", content_type="text/csv")
    assert r.get_json()["rows_checked"] == 2


def test_lineage_cycle_blocked(client):
    a = make(client, "a").get_json()["id"]
    b = make(client, "b").get_json()["id"]
    assert client.post(f"/api/products/{b}/lineage", json={"upstream_id": a}).status_code == 200
    assert client.post(f"/api/products/{a}/lineage", json={"upstream_id": b}).status_code == 409
