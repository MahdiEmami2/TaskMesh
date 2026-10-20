# Security

## Applied measures

1. **No arbitrary code execution.** Clients submit a `task_type` that must be
   registered in the handler registry; payloads are validated with Pydantic
   schemas. No `eval`/`exec`, no dynamic imports, no shell execution — verified
   by a dedicated test (`test_no_arbitrary_code_execution`).
2. **Input validation.** All endpoints validate types, enums, lengths and
   ranges via Pydantic; unknown statuses/priorities/task types return 422.
3. **Payload size limit.** Serialized payloads above the configured limit are
   rejected with 413 (protects DB and memory).
4. **Safe error surfaces.** Public API responses never include internal
   exception details; a global handler converts unhandled exceptions to a
   generic 500. Persisted errors are sanitized (single line, truncated).
5. **Operational endpoint protection.** When `TASKMESH_API_TOKEN` is set,
   cancel/retry require `Authorization: Bearer <token>`. Unset = open, for
   local development only. Documented, no credentials shipped.
6. **CORS.** Narrow allowlist via `TASKMESH_CORS_ORIGINS` (default
   `http://localhost:8080`); only GET/POST and two headers allowed.
7. **No secrets in the repo.** `.env.example` contains placeholders only;
   `.gitignore` excludes any populated `.env`.
8. **Resource hygiene.** DB sessions use context managers with rollback-on-
   error and close-on-exit; pool `pre_ping` handles stale connections.

## Security checklist (deployment)

- [ ] Set `TASKMESH_API_TOKEN` and put the API behind TLS before any shared deployment.
- [ ] Use strong, rotated PostgreSQL/Redis credentials (not the dev defaults).
- [ ] Restrict PostgreSQL/Redis to the internal network; never expose 5432/6379 publicly.
- [ ] Keep `TASKMESH_CORS_ORIGINS` as narrow as the actual dashboard origin.
- [ ] Review payloads at the application boundary if untrusted clients submit tasks (size limits and schema validation are enforced, but payload *content* semantics are handler-specific).
- [ ] Run with least-privilege DB users where possible (runtime needs DML only; migrations need DDL).

## Known limitations

- Bearer-token auth is a single shared static token; it is not user management.
- No rate limiting on the API (a deployment-level concern, e.g. reverse proxy).
- Attempt/error history keeps a truncated message, not a full forensic trace.
