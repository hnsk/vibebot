# Follow-up: Ergo Docker integration tests

Deferred from multi-network auth work (2026-04-18). Mock ircd covers RFC1459-only + Q-bot paths; this task adds a real-server SASL validation layer.

## Goal

Validate SASL PLAIN, EXTERNAL (CERTFP), and SCRAM-SHA-256 against a real IRCv3 daemon. Also exercise Ergo's built-in NickServ and soft/hard auth-failure modes.

**Rule: no live networks.** Local Ergo in Docker only. If Ergo cannot reproduce a path, extend `tests/fixtures/mock_ircd.py` instead.

## Scope

- Compose file `tests/integration/docker-compose.yml` pinning `ghcr.io/ergochat/ergo:stable`
- Bootstrap script seeding accounts:
  - `sasl-plain-user` / password
  - `sasl-scram-user` / password
  - `sasl-ext-user` + registered client cert fingerprint (via `NS CERT ADD`)
- Client cert generated at test setup (`openssl req -x509 -newkey ed25519 ...`)
- pytest marker `integration`; skip entire module if `docker` binary absent or `VIBEBOT_INT=1` not set
- Fixtures start/stop container once per test session

## Cases

1. SASL PLAIN → 903 success → JOIN `#test`
2. SASL SCRAM-SHA-256 → 903 → JOIN
3. SASL EXTERNAL with generated cert → 903 → JOIN
4. NickServ (`auth.method: nickserv`) → IDENTIFY → JOIN
5. Soft-fail: bad password, `required=false` → WARN logged, still joined
6. Hard-fail: bad password, `required=true` → disconnect

## Files

- `tests/integration/__init__.py`
- `tests/integration/docker-compose.yml`
- `tests/integration/ergo.yaml` (Ergo config: enable SASL mechanisms, register accounts on boot)
- `tests/integration/conftest.py` (session-scoped ergo container fixture)
- `tests/integration/test_sasl_real.py`
- `tests/integration/certs/` (gitignored; generated at fixture setup)

## Non-goals

- QuakeNet Q-bot flow (mock already covers it; no Q bot in Ergo)
- RFC1459 CAP-less servers (mock covers it; Ergo is IRCv3-first)
- CI wiring — opt-in only until Docker-in-CI is set up

## Verification

- `VIBEBOT_INT=1 pytest tests/integration/` all green on a machine with Docker
- `pytest` without the env var still runs the regular suite, skips integration module
