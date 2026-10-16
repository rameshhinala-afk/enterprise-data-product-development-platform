# Enterprise Data Product Development Platform

A simple base platform for creating, governing and publishing data products.

## Features
- **Catalog** – name, domain, owner, version, search and filters
- **Schema contract** – typed columns with nullability
- **Quality rules** – `not_null`, `unique`, `range`, `regex`; run against JSON or CSV samples
- **Lineage** – upstream/downstream links with circular-dependency protection
- **Lifecycle** – `draft → published → deprecated`; publishing requires a schema and a quality score ≥ 90
- **Web UI** at `/` and a JSON REST API

## Run
```bash
pip install -r requirements.txt
python app.py          # http://localhost:5000
pytest                 # run tests
```

## API
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/products` | list (filters: `q`, `domain`, `status`, `owner`) / create |
| GET/PUT/DELETE | `/api/products/<id>` | read / update / delete |
| POST | `/api/products/<id>/status` | `{"status": "published"}` |
| POST | `/api/products/<id>/validate` | `{"rows":[...]}` or CSV body |
| GET | `/api/products/<id>/quality` | recent quality runs |
| GET/POST | `/api/products/<id>/lineage` | view / add `{"upstream_id": n}` |
| GET | `/api/stats` | dashboard numbers |

## Layout
```
app.py              Flask app, SQLite storage, validation engine
templates/index.html  Single-page UI
test_app.py         Pytest tests
```

## Ideas for extension
Authentication/RBAC, versioned contracts, connectors (Postgres, S3), scheduled quality runs, PostgreSQL instead of SQLite.
