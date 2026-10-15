# Employee Attendance & Analytics API

FastAPI service backed by MongoDB 6.0+. Set `MONGO_URI` and `MONGO_DB` in the environment, install dependencies with `pip install -r requirements.txt`, then start from this directory:

```bash
uvicorn app.main:app --port 8000
```

The app creates its indexes on startup. `GET /health` pings MongoDB. The API contract and rules are in `openapi.yaml`; the review findings and implementation decisions are in `REVIEW.md` and `DECISIONS.md`.
