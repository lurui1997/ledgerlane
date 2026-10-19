# Demo video script

Target length: about 2 minutes 30 seconds. Hard cap: 3 minutes. Judges are not required to watch past that.

Record the simulator in a desktop browser at `http://localhost:4317` after `docker compose up`. No background music. No logos or wordmarks you do not own. Say "Alexa Plus" if you are unsure about showing the Alexa+ name on screen; the hackathon also asks the video to show the simulated experience, so a plain text label already in the page header is enough. Do not show API keys, the demo client secret, or a terminal full of tokens.

Speak a little slower than conversation. The word counts below are about 145 words per minute.

## Shot 1 — Title (0:00–0:08)

On screen: the Ledgerlane header and the empty conversation. Do not scroll yet.

Narration: "Shoppers ask an assistant to buy something. The risky part is not the chat. It is what happens when the card fails halfway through."

## Shot 2 — The ask (0:08–0:28)

Click the suggestion "Find trail running shoes under 120 dollars." Wait until the product cards appear. Point at a price, not at the code.

Narration: "This is a web simulation of an Alexa Plus conversation, which the hackathon allows without a device. The assistant is not answering from a prompt. It is calling a commerce MCP server over Streamable HTTP."

## Shot 3 — Compare and cart (0:28–0:48)

Click "Compare the two cheapest," then "Add the cheaper pair to my cart." Keep the ledger visible.

Narration: "Each tool returns a stable id the next tool accepts: a sku, then a cart. The ledger on the right records who called the tool and why, before the assistant speaks."

## Shot 4 — Confirm, then charge (0:48–1:12)

Click "Check out." Pause on the total. Then click "Yes, place the order." When the order card appears, point at the order id.

Narration: "Checkout only prices the cart. Nothing is captured until I say yes. Place order is a saga: reserve stock, authorize, create the order, capture, then open a shipment. The same request key cannot charge twice."

## Shot 5 — Track (1:12–1:22)

Click "Where is my order?"

Narration: "Tracking reads that order back. The assistant only speaks ids the tool returned."

## Shot 6 — Failed capture (1:22–1:55)

Click "Play a failed capture." Let the four lines run. When the reply says the checkout was rolled back, scroll the ledger to `capture_payment.failed` and the compensated rows.

Narration: "Same flow, but the card capture fails on purpose. The gateway voids the authorization, cancels the order, and puts the stock back. The ledger is the proof, in reverse order."

## Shot 7 — Close (1:55–2:20)

Stay on the ledger. Do not open an IDE unless a judge specifically needs the code; if you do, show `place_order` in `src/acmg/commerce.py` for three seconds and come back.

Narration: "The server speaks MCP revision 2025-11-25. Payments are a mock provider today, behind an interface the PayPal sandbox can implement next. Postgres keeps the audit log. The demo runs with no model key."

Stop the recording. Upload to YouTube or Vimeo as public, unlisted is not enough if the hackathon requires public. Paste the link into the Devpost form. Trim anything past 2:40 rather than adding a second take of the same click.
