# Devpost draft — Amazon Developer Hackathon "Build, Ship, Shape"

Draft only. Do not submit this file for the entrant. Replace every `FILL:` before pasting into the form. Official rules: https://amazonappdev2026.devpost.com/rules (checked 2026-10-19). Deadline on that page: Friday, October 23, 2026, 12:00 pm Pacific Time.

All submission text must be English. This draft is English.

## Title

Ledgerlane: an agentic commerce MCP gateway

## Tagline

A shopping assistant that can take the order back when the card fails, and prove every step.

(123 characters or fewer is a safe Devpost tagline length. This line is under 110.)

## Primary track

Alexa+

The project is both of the Alexa+ options the rules allow: a self-hosted MCP server on spec 2025-11-25 over Streamable HTTP, and a web app that simulates the Alexa+ conversation. The simulator calls that MCP server at runtime.

## Mini challenges

Open Source — only if you publish this repository (Apache-2.0) during the hackathon window and fill the extra fields below.

Do not enter AWS Builder. This build does not call Bedrock, AgentCore, Strands, SageMaker, or Kiro.

The rules describe the Open Source mini challenge as a new, additional open-source project alongside the primary submission. One repository may be treated as the primary project rather than as an extra contribution. If you only have this repo, either skip the mini challenge or say plainly that the primary entry is the new open-source project. Do not imply a second URL you do not have.

## Built with

- Python
- MCP Python SDK (Streamable HTTP, protocol 2025-11-25)
- FastAPI
- Postgres 16
- Docker Compose
- A mock LLM planner, with an OpenAI-compatible adapter for OpenAI or Gemini later

Alexa+ web simulation. Not the Alexa AI CLI, not a certified add-on.

## Inspiration

Voice shopping dies in the gap between "yes" and a receipt. A model can pick a product. It cannot safely reserve stock, capture a card, and undo itself when the capture fails, unless something behind the tool call keeps a ledger. I wanted that something to be a small open-source gateway an Alexa+ agent can call, with the same shape a PayPal sandbox can plug into later.

## What it does

Ledgerlane exposes commerce tools over MCP: search a catalog, compare options, build a cart, price a checkout, place an order, track it, and refund it. Place and refund are sagas. If a step fails, completed steps compensate in reverse, so a failed card capture voids the authorization and puts the stock back. Every tool call is written to Postgres with who, what, and why, and a turn can be replayed from that log. Idempotency keys stop a retry from capturing twice.

The page at `/` is the Alexa+ simulation. Suggested lines run a full order and a failed capture with no API key.

## How we built it

The gateway is one Python service. FastMCP serves Streamable HTTP on `/mcp` and negotiates protocol 2025-11-25. Tools call a commerce service. The saga runner and the audit log are plain code over Postgres, covered by tests that do not need a model. The simulator's agent loop is an MCP client: it lists tools and calls them over HTTP, it does not import the checkout functions directly. The planner is mock by default. `LLM_PROVIDER=openai` or `gemini` switches the HTTP client; keys come from the environment.

Alexa+ discovery is sketched for later account linking: unauthenticated `/mcp` returns 401 without `WWW-Authenticate`, and the well-known OAuth documents advertise PKCE S256. The demo token is a local stand-in, not Login with Amazon.

The catalog is fictional on purpose, so the demo video does not depend on someone else's trademarks.

## Challenges we ran into

- Alexa+ production onboarding (Alexa AI CLI, a public HTTPS URL, icons, privacy policy, certification) is a different path from the hackathon's web-simulation path. The MCP server is real; it is not yet an Alexa+ add-on, because that needs the entrant's developer account.
- The MCP Python SDK's built-in bearer middleware emits `WWW-Authenticate`. Current Alexa+ docs say that header is not supported yet and that a bare 401 is required. The gateway uses its own gate so the header is absent.
- A single process must accept the simulator's HTTP call and, inside that call, accept the MCP callback. The agent uses an async client so the event loop can serve both.
- Idempotency has to be decided before the saga, and a refund replay has to win over the "already refunded" check, or a retry looks like a second business error instead of the original receipt.

## Accomplishments that we're proud of

- A checkout that rolls back on a failed capture, with the compensating actions in the audit log in reverse order.
- The same idempotency key returns the same order and does not authorize the card again.
- The demo, including that failure, runs from Docker Compose with no model key and no payment key.
- Tool results are stable ids, so search, compare, cart, quote, and order chain the way Alexa+'s tool guidance asks.

## What we learned

An agent that can spend money needs a workflow engine more than it needs a longer prompt. The model should choose the next tool. The server should own confirmation, idempotency, and undo. The audit log is the only way to explain a charge after the conversation is gone.

The hackathon's simulation path is enough to show the interaction. Connecting the same server to Alexa+ still means OAuth, a public URL, and the add-on package. Those are deployment steps, not a reason to skip the protocol.

## What's next

- Register the MCP server as an Alexa+ add-on once an Alexa developer account and a public HTTPS endpoint are available. Add the privacy policy and terms URLs the manifest requires.
- Implement `PayPalSandboxProvider` against the sandbox Orders v2 API for the PayPal AI Hackathon (authorize, capture, void, refund). Leave that as a visible delta during that event's window.
- Point `LLM_PROVIDER` at Gemini when the lablab agents hackathon dates are real.
- Chinese docs, a maintenance roadmap, and CI for the Beijing open-source track. Apache-2.0 stays the license. Note: a later event that requires MIT would need a dual-license decision; this repo is Apache-2.0.
- Resume an `in_progress` saga after a crash, instead of only refusing a second capture.

## What was built during the hackathon

The submission window opened August 31, 2026. This repository did not exist before October 19, 2026. The MCP server, saga runner, Postgres audit log, mock payments, payment-provider seam, web simulator, tests, and these docs were written in that window. There is no pre-hackathon codebase hiding behind a new README.

## Product feedback

Required for each tool, API, or SDK you actually used. Draft answers below. Edit them in your own voice before submitting. Do not claim you used the Alexa AI CLI or AWS if you did not.

### MCP specification 2025-11-25 and Streamable HTTP

- Used for: the commerce tool surface Alexa+ (and the web simulator) calls.
- What worked: one HTTP endpoint, JSON or SSE responses, and tool results that carry stable ids. The Python SDK negotiated `2025-11-25` without a custom handshake.
- What needs work: account-linking docs require a 401 with no `WWW-Authenticate` header, while the SDK's bearer middleware adds that header. DNS-rebinding defaults are right for localhost and easy to get wrong once a tunnel hostname appears. The certification note that security policy details are "in a future revision" makes it hard to know whether a demo token is acceptable outside the hackathon simulation path.
- Onboarding: getting a local tool call working was straightforward. Getting from that to a certified add-on is not, because it depends on the Alexa AI CLI, media assets, and a public URL that this submission does not have yet.
- Build again: yes. The protocol is the right boundary between the assistant and checkout. I would not put saga state inside the prompt.

### MCP Python SDK

- Used for: the Streamable HTTP server and the client inside the simulator's agent loop.
- What worked: `FastMCP` tool schemas, JSON responses, and `streamable_http_client` were enough to list tools and call them from the same service.
- What needs work: mounting the session manager inside another ASGI app fights the "run the session manager once" rule. Stateless mode plus a request-scoped user identity is easy to get wrong if the tool runs on a spawned task. Built-in auth does not match the Alexa+ 401 shape.
- Onboarding: pip install and a first tool was a short path. Combining it with our own auth and Postgres took longer than the quickstart implies.
- Build again: yes, for the protocol implementation. I would still keep commerce rules in our own code.

### FastAPI, Postgres, Docker Compose

- Used for: the simulator API, the audit log, and one-command startup.
- What worked: Compose health checks made the gateway wait for Postgres. The audit table is ordinary SQL, which is what I want when I have to replay a charge.
- What needs work: nothing specific enough to file as a platform bug.
- Onboarding: uneventful.
- Build again: yes.

### Alexa+ add-on tooling (not used)

I read the MCP quickstart and the functional requirements. I did not install the Alexa AI CLI or deploy an add-on, because that requires an Alexa developer account and a publicly reachable HTTPS URL. Feedback on that CLI would be invented. The friction log below is about the docs, not about a CLI session I did not run.

## Optional feature requests

1. Document a hackathon-acceptable auth mode for MCP servers that are demonstrated only in the web simulator, distinct from the certification 401/PKCE checklist. Priority: important.
2. Publish the security and data-policy section the quickstart still marks as a future revision, before teams design what the audit log is allowed to store. Priority: important.
3. A way for an MCP tool to return a customer-safe spoken line and a structured payload without the SDK wrapping exceptions as `Error executing tool <name>: ...`, which leaks the tool name into text a careless agent might speak. Priority: nice-to-have.

## Optional friction log

Participants who submit a friction log can receive up to a 10% bonus. Only include entries you can stand behind. Draft:

### Entry 1

- Task: confirm how an Alexa+ MCP server must authenticate.
- Steps: read the hackathon rules for the simulation path, then the Alexa+ MCP quickstart authentication checklist.
- Expected: one story for a hackathon MCP server.
- Actual: the rules allow a web simulation with any agent stack, and also a self-hosted MCP server on spec 2025-11-25. The quickstart, aimed at add-on certification, requires a bare 401, protected-resource metadata, and PKCE S256, and says `WWW-Authenticate` is not supported. The Python SDK's auth middleware emits that header.
- Severity: medium.
- Workaround: custom 401 gate with no authenticate header, plus demo OAuth metadata, and the web simulator for the interaction.
- Suggestion: add a "hackathon simulation vs certification" box at the top of the quickstart, and show a 401 example with the header explicitly absent.

### Entry 2

- Task: decide whether the Open Source mini challenge can be the same repository as the Alexa+ entry.
- Steps: read the mini-challenge wording "new, additional open-source project ... alongside a primary track submission."
- Expected: a new Apache-licensed repo would qualify on its own.
- Actual: "additional" can be read as a second artifact. The form also asks for a contribution URL, a project URL, and a GitHub username.
- Severity: low.
- Workaround: treat Open Source as optional until the entrant has a public URL, and describe this repo as the primary entry.
- Suggestion: say whether the primary repo counts when the primary project is itself the new open-source work.

## Open Source mini-challenge fields

Fill these only if you enter the mini challenge.

- Contribution URL: `FILL: public repository URL`
- Project repository URL: `FILL: same URL if this repo is the contribution`
- GitHub username: `FILL:`
- What you did: Created Ledgerlane, an Apache-2.0 MCP server that runs saga-style checkout and refund with a Postgres audit log, plus a web simulator that calls the server over Streamable HTTP.
- How it works: See `docs/ARCHITECTURE.md`.
- Why it matters: Agents that can pay need idempotency, compensation, and a replayable record. Those do not belong only inside one hackathon's sample app.

## Testing instructions for judges

```bash
docker compose up --build
```

Open `http://localhost:4317`. Click "Play a full order," then "Play a failed capture." No account and no API key.

The MCP endpoint is `http://localhost:4317/mcp`. Unauthenticated calls return 401. The demo bearer token is `demo-shopper-token`, documented in `.env.example` and the README. It is a local demo credential, not a secret from a real user.

## Links to paste

- GitHub: `FILL:` (public, Apache-2.0 file at the repo root, or private and shared with the GitHub users named in the rules)
- Demo video: `FILL:` (YouTube or Vimeo, public, under 3 minutes, English)
- Built during the hackathon: yes, entirely, starting 2026-10-19

## Checklist before you click submit

- [ ] Devpost account is yours. This draft was not submitted.
- [ ] Repo is public with `LICENSE`, or private with the reviewer invites accepted (invites expire after 7 days).
- [ ] Video is public, under 3 minutes, and shows the simulator actually placing an order and rolling one back.
- [ ] Product feedback is in your words.
- [ ] You did not claim AWS Builder.
- [ ] You are eligible under the rules (age of majority where you live; not in an excluded territory). This draft does not decide that.
