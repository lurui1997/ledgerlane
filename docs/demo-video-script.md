# Demo video script

Finished cut: **2:00**. Hard cap: 3 minutes. The file is a 1920×1080 H.264/AAC recording of the simulator in mock mode, with an English voiceover (Piper, Lessac) and English captions. No music. No logos.

Recorded from `http://127.0.0.1:4317` after the gateway was up on Postgres with `LLM_PROVIDER=mock` and `PAYMENT_PROVIDER=mock`. The title card is the only generated frame. Everything after it is the running app.

Say "Alexa Plus" in the narration. The title card shows the plain-text label `Alexa+ track (web simulation)`. Do not show a model key. The 401 body mentions the demo bearer on purpose. It is the local credential from `.env.example`, not a production secret.

## Shot list

| Time | Picture | Narration |
| --- | --- | --- |
| 0:00–0:07 | Title card. Project name and `Alexa+ track (web simulation)`. | "Ledgerlane. An agentic commerce gateway for the Alexa Plus track, shown here as a web simulation." |
| 0:07–0:16 | Empty simulator. Store, payments, and model pills read postgres, mock, mock. | "Shoppers ask an assistant to buy something. The risky part is not the chat. It is what happens when the card fails halfway through." |
| 0:16–0:27 | Chip: find trail running shoes under 120 dollars. Two cards, $98 and $108. Ledger records the search. | "This is a web simulation, which the hackathon allows without a device. The assistant is not answering from a prompt. It calls a commerce MCP server over Streamable HTTP." |
| 0:27–0:35 | Compare the two cheapest. | "Each tool returns a stable id the next tool accepts. Comparing the two cheapest shoes names the lower price and the higher rating." |
| 0:35–0:43 | Add the cheaper pair. Cart total $98. | "The cheaper pair goes into a cart. The ledger on the right records who called the tool, and why, before the assistant speaks." |
| 0:43–0:49 | Check out. The reply says nothing is charged until you confirm. | "Checkout only prices the cart. Nothing is captured until I say yes." |
| 0:49–1:00 | Yes, place the order. Order id, tracking number, and the saga through capture and shipment. | "Place order is a saga: reserve stock, authorize, create the order, capture, then open a shipment. The same request key cannot charge twice." |
| 1:00–1:05 | Where is my order? | "Tracking reads that order back. The assistant only speaks ids the tool returned." |
| 1:05–1:15 | Fresh session. Play a failed capture: lamp, cart, checkout, then a capture that fails. The reply says the checkout was rolled back. | "Same flow, but the card capture fails on purpose. The gateway voids the authorization, cancels the order, and puts the stock back." |
| 1:15–1:25 | Ledger scrolled to `saga.capture_payment.failed` and the compensated rows. The order card says cancelled. | "The ledger is the proof, in reverse order. Capture failed. Then the order, the authorization, and the stock reservation are compensated." |
| 1:25–1:34 | `/audit?session_id=...` replays `GET /api/audit`. | "The same events replay from Postgres. Every row names the actor, the action, and the reason." |
| 1:34–1:46 | Live `POST /mcp` with no token. HTTP 401. No `WWW-Authenticate` header. | "An unauthenticated call to the MCP endpoint returns 401, with no WWW-Authenticate header. That is the discovery shape Alexa Plus publishes." |
| 1:46–2:00 | Architecture slide: shopper, simulator, agent, MCP, saga, Postgres, mock payments, PayPal seam not wired. | "Shopper, simulator, agent, MCP server, saga, and a payment provider. The provider is a mock today. A PayPal sandbox can sit behind the same interface. No model key required." |

Upload the finished file to YouTube or Vimeo as **public** if the hackathon requires a public link. Unlisted is not enough when the rules say public. Paste the link into the Devpost form. Do not upload until you have approved the cut.
