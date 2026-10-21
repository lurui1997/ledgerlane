# Contributing

Ledgerlane is Apache-2.0. By contributing, you agree that your work is licensed under the same terms.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The unit tests use the in-memory store. They do not need Docker or an API key.

The demo stack is Postgres plus the gateway, both in mock mode:

```bash
docker compose up --build
```

Open http://localhost:4317. `/audit?session_id=...` replays the audit log for a simulator session.

## Secrets

Do not commit `.env`, cloud credentials, or a real PayPal or model key. `.env.example` and the Compose file contain local demo values (`acmg` / `demo-shopper-token` / `demo-secret`). Those are for a laptop demo. Replace them before any host that is not yours.

## Changes that fit

- Keep money movement behind `confirm=true`.
- A new payment provider implements `PaymentProvider` in `src/acmg/payments.py`. Do not special-case it inside the saga.
- A new planner implements the same decision shape as `MockLLM`. Read the key from the environment.
- Record agent actions through the audit log, with an actor and a reason.

## Checks before a pull request

`pytest` passes. If you changed the simulator page, try both a desktop window and a narrow one. Do not add third-party logos.
