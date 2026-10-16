"""Enterprise Data Product Development Platform - base project.

Features
- Data product catalog (domain, owner, version, lifecycle status)
- Schema contract per product
- Data quality rules + validation runs against sample data (JSON or CSV)
- Lineage (upstream/downstream) with cycle protection
- Publish workflow: draft -> published -> deprecated
"""
import csv
import io
import json
import re
import sqlite3
from datetime import date, datetime, timezone

from flask import Flask, g, jsonify, render_template, request

DB_PATH = "platform.db"
VALID_TYPES = {"string", "int", "float", "bool", "date"}
VALID_STATUS = {"draft", "published", "deprecated"}

app = Flask(__name__)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    domain TEXT NOT NULL,
    owner TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    version TEXT NOT NULL DEFAULT '0.1.0',
    schema_json TEXT NOT NULL DEFAULT '[]',
    rules_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lineage (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    upstream_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, upstream_id)
);
CREATE TABLE IF NOT EXISTS quality_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    rows_checked INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    score REAL NOT NULL,
    details_json TEXT NOT NULL,
    run_at TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config.get("DB_PATH", DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(app.config.get("DB_PATH", DB_PATH))
    db.executescript(SCHEMA_SQL)
    db.commit()
    db.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_to_product(r):
    d = dict(r)
    d["schema"] = json.loads(d.pop("schema_json"))
    d["rules"] = json.loads(d.pop("rules_json"))
    return d


def err(msg, code=400):
    return jsonify({"error": msg}), code


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def validate_schema(columns):
    if not isinstance(columns, list):
        return "schema must be a list of columns"
    seen = set()
    for c in columns:
        if not isinstance(c, dict) or not c.get("name"):
            return "each column needs a name"
        if c["name"] in seen:
            return f"duplicate column: {c['name']}"
        seen.add(c["name"])
        if c.get("type", "string") not in VALID_TYPES:
            return f"invalid type for {c['name']} (use {sorted(VALID_TYPES)})"
    return None


def validate_rules(rules, columns):
    names = {c["name"] for c in columns}
    for r in rules:
        if r.get("column") not in names:
            return f"rule references unknown column: {r.get('column')}"
        if r.get("type") not in {"not_null", "unique", "range", "regex"}:
            return f"unknown rule type: {r.get('type')}"
    return None


def cast(value, typ):
    """Return (ok, casted_value)."""
    if value is None or value == "":
        return True, None
    try:
        if typ == "int":
            return True, int(value)
        if typ == "float":
            return True, float(value)
        if typ == "bool":
            if isinstance(value, bool):
                return True, value
            if str(value).lower() in ("true", "1", "yes"):
                return True, True
            if str(value).lower() in ("false", "0", "no"):
                return True, False
            return False, None
        if typ == "date":
            return True, date.fromisoformat(str(value))
        return True, str(value)
    except (ValueError, TypeError):
        return False, None


def run_quality(columns, rules, rows):
    """Check rows against schema contract and quality rules."""
    issues = []
    col_types = {c["name"]: c.get("type", "string") for c in columns}
    nullable = {c["name"]: c.get("nullable", True) for c in columns}
    unique_seen = {r["column"]: set() for r in rules if r["type"] == "unique"}
    failed_rows = set()

    for i, row in enumerate(rows):
        for name, typ in col_types.items():
            ok, val = cast(row.get(name), typ)
            if not ok:
                issues.append({"row": i, "column": name, "check": "type", "detail": f"not a valid {typ}"})
                failed_rows.add(i)
            elif val is None and not nullable[name]:
                issues.append({"row": i, "column": name, "check": "nullable", "detail": "null not allowed"})
                failed_rows.add(i)
        for r in rules:
            col, kind = r["column"], r["type"]
            raw = row.get(col)
            ok, val = cast(raw, col_types[col])
            if kind == "not_null" and (raw is None or raw == ""):
                issues.append({"row": i, "column": col, "check": kind, "detail": "value is null"})
                failed_rows.add(i)
            elif kind == "unique" and raw not in (None, ""):
                if raw in unique_seen[col]:
                    issues.append({"row": i, "column": col, "check": kind, "detail": f"duplicate {raw!r}"})
                    failed_rows.add(i)
                unique_seen[col].add(raw)
            elif kind == "range" and ok and val is not None:
                if ("min" in r and val < r["min"]) or ("max" in r and val > r["max"]):
                    issues.append({"row": i, "column": col, "check": kind,
                                   "detail": f"{val} outside [{r.get('min')}, {r.get('max')}]"})
                    failed_rows.add(i)
            elif kind == "regex" and raw not in (None, ""):
                if not re.fullmatch(r.get("pattern", ".*"), str(raw)):
                    issues.append({"row": i, "column": col, "check": kind, "detail": "pattern mismatch"})
                    failed_rows.add(i)

    total = len(rows)
    passed = total - len(failed_rows)
    score = round(passed / total * 100, 1) if total else 100.0
    return {"rows_checked": total, "passed": passed, "score": score, "issues": issues[:200]}


def would_create_cycle(db, product_id, upstream_id):
    """True if upstream_id already depends (transitively) on product_id."""
    stack, seen = [upstream_id], set()
    while stack:
        cur = stack.pop()
        if cur == product_id:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack += [r["upstream_id"] for r in db.execute(
            "SELECT upstream_id FROM lineage WHERE product_id=?", (cur,))]
    return False


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/products")
def list_products():
    db = get_db()
    q = "SELECT * FROM products WHERE 1=1"
    args = []
    for field in ("domain", "status", "owner"):
        if request.args.get(field):
            q += f" AND {field}=?"
            args.append(request.args[field])
    if request.args.get("q"):
        q += " AND (name LIKE ? OR description LIKE ?)"
        args += [f"%{request.args['q']}%"] * 2
    rows = db.execute(q + " ORDER BY updated_at DESC", args).fetchall()
    out = []
    for r in rows:
        p = row_to_product(r)
        last = db.execute("SELECT score FROM quality_runs WHERE product_id=? ORDER BY id DESC LIMIT 1",
                          (p["id"],)).fetchone()
        p["quality_score"] = last["score"] if last else None
        out.append(p)
    return jsonify(out)


@app.post("/api/products")
def create_product():
    data = request.get_json(silent=True) or {}
    for f in ("name", "domain", "owner"):
        if not data.get(f):
            return err(f"{f} is required")
    columns, rules = data.get("schema", []), data.get("rules", [])
    msg = validate_schema(columns) or validate_rules(rules, columns)
    if msg:
        return err(msg)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO products (name, domain, owner, description, version, schema_json, rules_json,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (data["name"], data["domain"], data["owner"], data.get("description", ""),
             data.get("version", "0.1.0"), json.dumps(columns), json.dumps(rules), now(), now()))
        db.commit()
    except sqlite3.IntegrityError:
        return err("a product with that name already exists", 409)
    return get_product(cur.lastrowid)[0], 201


@app.get("/api/products/<int:pid>")
def get_product(pid):
    r = get_db().execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not r:
        return err("not found", 404)
    return jsonify(row_to_product(r)), 200


@app.put("/api/products/<int:pid>")
def update_product(pid):
    db = get_db()
    r = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not r:
        return err("not found", 404)
    cur = row_to_product(r)
    data = request.get_json(silent=True) or {}
    for f in ("name", "domain", "owner", "description", "version", "schema", "rules"):
        if f in data:
            cur[f] = data[f]
    msg = validate_schema(cur["schema"]) or validate_rules(cur["rules"], cur["schema"])
    if msg:
        return err(msg)
    try:
        db.execute(
            "UPDATE products SET name=?, domain=?, owner=?, description=?, version=?, schema_json=?,"
            " rules_json=?, updated_at=? WHERE id=?",
            (cur["name"], cur["domain"], cur["owner"], cur["description"], cur["version"],
             json.dumps(cur["schema"]), json.dumps(cur["rules"]), now(), pid))
        db.commit()
    except sqlite3.IntegrityError:
        return err("a product with that name already exists", 409)
    return get_product(pid)


@app.delete("/api/products/<int:pid>")
def delete_product(pid):
    db = get_db()
    db.execute("DELETE FROM products WHERE id=?", (pid,))
    db.commit()
    return "", 204


@app.post("/api/products/<int:pid>/status")
def set_status(pid):
    """Lifecycle: draft -> published -> deprecated. Publishing needs a contract + passing quality."""
    db = get_db()
    r = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not r:
        return err("not found", 404)
    p = row_to_product(r)
    target = (request.get_json(silent=True) or {}).get("status")
    if target not in VALID_STATUS:
        return err(f"status must be one of {sorted(VALID_STATUS)}")
    if target == "published":
        if not p["schema"]:
            return err("define a schema contract before publishing")
        last = db.execute("SELECT score FROM quality_runs WHERE product_id=? ORDER BY id DESC LIMIT 1",
                          (pid,)).fetchone()
        if not last or last["score"] < 90:
            return err("publishing requires a quality run with score >= 90")
        for up in db.execute("SELECT p.name, p.status FROM lineage l JOIN products p ON p.id=l.upstream_id"
                             " WHERE l.product_id=?", (pid,)):
            if up["status"] == "deprecated":
                return err(f"upstream product '{up['name']}' is deprecated")
    db.execute("UPDATE products SET status=?, updated_at=? WHERE id=?", (target, now(), pid))
    db.commit()
    return get_product(pid)


@app.post("/api/products/<int:pid>/validate")
def validate_product(pid):
    """Body: JSON {"rows": [...]}  or  text/csv with a header row."""
    db = get_db()
    r = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not r:
        return err("not found", 404)
    p = row_to_product(r)
    if request.is_json:
        rows = (request.get_json(silent=True) or {}).get("rows")
    else:
        rows = list(csv.DictReader(io.StringIO(request.get_data(as_text=True))))
    if not isinstance(rows, list):
        return err("provide rows as JSON {'rows': [...]} or CSV text")
    if not p["schema"]:
        return err("product has no schema contract")
    result = run_quality(p["schema"], p["rules"], rows)
    db.execute("INSERT INTO quality_runs (product_id, rows_checked, passed, score, details_json, run_at)"
               " VALUES (?,?,?,?,?,?)",
               (pid, result["rows_checked"], result["passed"], result["score"],
                json.dumps(result["issues"]), now()))
    db.commit()
    return jsonify(result)


@app.get("/api/products/<int:pid>/quality")
def quality_history(pid):
    rows = get_db().execute(
        "SELECT id, rows_checked, passed, score, run_at FROM quality_runs WHERE product_id=?"
        " ORDER BY id DESC LIMIT 20", (pid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/products/<int:pid>/lineage")
def get_lineage(pid):
    db = get_db()
    up = db.execute("SELECT p.id, p.name, p.status FROM lineage l JOIN products p ON p.id=l.upstream_id"
                    " WHERE l.product_id=?", (pid,)).fetchall()
    down = db.execute("SELECT p.id, p.name, p.status FROM lineage l JOIN products p ON p.id=l.product_id"
                      " WHERE l.upstream_id=?", (pid,)).fetchall()
    return jsonify({"upstream": [dict(r) for r in up], "downstream": [dict(r) for r in down]})


@app.post("/api/products/<int:pid>/lineage")
def add_lineage(pid):
    db = get_db()
    up = (request.get_json(silent=True) or {}).get("upstream_id")
    if not isinstance(up, int) or up == pid:
        return err("upstream_id must be a different product id")
    found = db.execute("SELECT COUNT(*) AS n FROM products WHERE id IN (?,?)", (pid, up)).fetchone()["n"]
    if found != 2:
        return err("both products must exist", 404)
    if would_create_cycle(db, pid, up):
        return err("this link would create a circular dependency", 409)
    db.execute("INSERT OR IGNORE INTO lineage (product_id, upstream_id) VALUES (?,?)", (pid, up))
    db.commit()
    return get_lineage(pid)


@app.get("/api/stats")
def stats():
    db = get_db()
    by_status = {r["status"]: r["n"] for r in db.execute(
        "SELECT status, COUNT(*) n FROM products GROUP BY status")}
    by_domain = {r["domain"]: r["n"] for r in db.execute(
        "SELECT domain, COUNT(*) n FROM products GROUP BY domain")}
    avg = db.execute("SELECT AVG(score) a FROM (SELECT score FROM quality_runs q WHERE id ="
                     " (SELECT MAX(id) FROM quality_runs WHERE product_id=q.product_id))").fetchone()["a"]
    return jsonify({"by_status": by_status, "by_domain": by_domain,
                    "avg_quality": round(avg, 1) if avg is not None else None})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
