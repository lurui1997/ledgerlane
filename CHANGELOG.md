# Changelog

## 0.1.0 — 2026-10-21

Everything in this release was built during the Amazon Developer Hackathon "Build, Ship, Shape" window. That window opened on 2026-08-31. The Alexa+ track deadline is 2026-10-23, 12:00 Pacific. There was no earlier codebase.

### Added

- Commerce MCP server on protocol 2025-11-25 over Streamable HTTP: search, product, compare, cart, checkout, place order, track, refund.
- Saga checkout. A failed capture voids the authorization, cancels the order, and releases reserved stock.
- Idempotency so a repeated place or refund returns the first result instead of charging again.
- Postgres audit log of who called which tool, and why, plus `GET /audit` to replay a session.
- Payment provider interface, a mock card, and a PayPal sandbox seam that does not call PayPal yet.
- Alexa+ web simulator with a mock planner. OpenAI-compatible and Gemini planners read keys from the environment.
- Demo OAuth metadata: HTTP 401 without `WWW-Authenticate`, protected-resource metadata, PKCE S256.
- Docker Compose, unit tests, Apache-2.0 license, and the docs under `docs/`.

### Submission pass, same window

- README quickstart for a fresh clone, screenshots, contributing guide, and this changelog.
- The simulator keeps a long conversation inside the panels instead of scrolling the page away.
- Checkout no longer draws a cart line with a missing total.
