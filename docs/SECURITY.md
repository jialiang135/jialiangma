# Security / Release Notes

## Current deployment boundary

The current server URL (`http://193.112.29.164:8080`) is plain HTTP. Passwords and bearer
JWTs are therefore not encrypted in transit. Do not use real credentials over an untrusted
network. Before treating the public deployment as production/enterprise-ready, terminate TLS
with a trusted certificate and use an HTTPS domain; then restrict `CORS_ORIGINS` to that origin.
The Nginx TLS block and Compose port mapping are currently templates, not active configuration.

Registration should remain disabled unless public account creation is explicitly intended.
Production must use unique `JWT_SECRET_KEY` and admin credentials in the server-only
`config/.env`; never put that file in source control or deployment archives.

## ChromaDB advisory exception

The current pin (`chromadb==1.5.9`) is reported by `pip-audit` for `PYSEC-2026-311`,
`PYSEC-2026-3813`, `PYSEC-2026-3814`, and `PYSEC-2026-3815`. At the time of this review,
the advisory database listed no fixed version. The reported attack paths target ChromaDB's
network API / authorization behavior. This application uses `chromadb.PersistentClient` for
an embedded local database and does not start or expose a ChromaDB server; the app container
only serves the FastAPI application through Nginx. Keep that boundary: do not enable or expose
a ChromaDB HTTP server until a patched release is available and the issue has been re-reviewed.
Re-run `pip-audit` after dependency updates.

## Optional RAGAS profile exceptions

The Dockerfile installs RAGAS by default using the separately pinned, `pip check`-clean profile
in `requirements-eval.txt`. `pip-audit` also reports two advisories for this optional stack, with
no fixed versions listed at review time:

- `PYSEC-2026-3046` (`ragas==0.4.3`) is an SSRF in its multimodal collection metric's local-file /
  URL processing. The application exposes only four text metrics (`faithfulness`, `answer_relevancy`,
  `context_precision`, `context_recall`) through an allowlist and does not call the vulnerable
  multimodal metric.
- `PYSEC-2026-2447` (`diskcache==5.6.3`) concerns pickle deserialization when an attacker can write
  to the cache directory. The application does not use DiskCache directly or expose a writable
  DiskCache path to clients.

These are compensating controls, not upstream fixes. Reassess after each RAGAS update; `pip-audit`
will fail CI for any new advisory not explicitly reviewed. `langchain-openai` is pinned to the
patched `1.1.14` in the evaluation profile to avoid its separately reported SSRF advisory.

## Release checks

- Backend: `.venv/bin/python -m ruff check .`, `.venv/bin/python -m pip check`, and
  `.venv/bin/python -m pytest tests/ -q -m 'not slow'`.
- Frontend: `cd frontend && npm ci && npm audit && npm test && npm run build`.
- Deployment: preserve the server-only `.env`, deploy one build at a time, and verify the
  health endpoint plus every asset referenced by the deployed `index.html`.

The task queue and retrieval caches are process-local, while SQLite/Chroma data are local
persistent state. Do not horizontally scale the app container without first replacing or
coordinating those components and validating data consistency.
