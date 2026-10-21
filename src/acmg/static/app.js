const thread = document.querySelector("#thread");
const ledger = document.querySelector("#ledger");
const chips = document.querySelector("#chips");
const form = document.querySelector("#composer");
const input = document.querySelector("#message");
const errorBox = document.querySelector("#error");
const pills = document.querySelector("#pills");

const sessionId = sessionStorage.getItem("ledgerlane-session") || crypto.randomUUID();
sessionStorage.setItem("ledgerlane-session", sessionId);

const suggestions = [
  "Find trail running shoes under 120 dollars",
  "Compare the two cheapest",
  "Add the cheaper pair to my cart",
  "Check out",
  "Yes, place the order",
  "Where is my order?",
];

const happyPath = [
  "Find trail running shoes under 120 dollars",
  "Compare the two cheapest",
  "Add the cheaper pair to my cart",
  "Check out",
  "Yes, place the order",
  "Where is my order?",
];

const failPath = [
  "Find a desk lamp",
  "Add the lamp to my cart",
  "Check out",
  "Yes, place the order, but make the card capture fail",
];

let busy = false;

suggestions.forEach((text) => {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  button.addEventListener("click", () => send(text));
  chips.append(button);
});

document.querySelector("#play-happy").addEventListener("click", () => play(happyPath));
document.querySelector("#play-fail").addEventListener("click", () => play(failPath));
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  send(text);
});

loadHealth();

async function loadHealth() {
  try {
    const response = await fetch("/health");
    const body = await response.json();
    pills.replaceChildren(
      pill(`Store: ${body.store}`),
      pill(`Payments: ${body.payment_provider}`),
      pill(`Model: ${body.llm_provider}`),
      pill("MCP 2025-11-25"),
    );
  } catch {
    pills.replaceChildren(pill("Gateway unreachable"));
  }
}

function pill(text) {
  const node = document.createElement("span");
  node.className = "pill";
  node.textContent = text;
  return node;
}

async function play(lines) {
  for (const line of lines) {
    await send(line);
    await wait(700);
  }
}

async function send(text) {
  if (busy) return;
  busy = true;
  errorBox.hidden = true;
  appendBubble("You", text, "shopper");
  const pending = appendBubble("Ledgerlane", "One moment…", "concierge");
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.detail || "The concierge could not finish that turn.");
    }
    pending.querySelector("p:last-child").textContent = body.reply;
    renderCards(pending, body.trace || []);
    renderLedger(body.ledger || []);
  } catch (error) {
    pending.remove();
    errorBox.hidden = false;
    errorBox.textContent = error.message || "The request failed.";
  } finally {
    busy = false;
    thread.scrollTop = thread.scrollHeight;
  }
}

function appendBubble(who, text, kind) {
  const article = document.createElement("article");
  article.className = `bubble ${kind}`;
  const name = document.createElement("p");
  name.className = "who";
  name.textContent = who;
  const body = document.createElement("p");
  body.textContent = text;
  article.append(name, body);
  thread.append(article);
  return article;
}

function renderCards(article, trace) {
  const cards = document.createElement("div");
  cards.className = "cards";
  trace.forEach((step) => {
    const result = step.result || {};
    (result.products || []).forEach((product) => cards.append(productCard(product)));
    if (result.quote_id) cards.append(receipt("Checkout", `${result.total} · quote ${result.quote_id}`));
    if (result.order_id && result.status) {
      cards.append(receipt(`Order ${result.order_id}`, `${result.status} · ${result.total || ""} ${result.tracking_number || ""}`.trim()));
    }
    if (result.cart_id && result.items && !result.quote_id && !result.order_id) {
      const total = result.subtotal || result.total || "";
      const lines = result.items.map((item) => `${item.qty} × ${item.name}`).join(", ");
      cards.append(receipt("Cart", total ? `${lines} · ${total}` : lines));
    }
  });
  if (cards.childElementCount) article.append(cards);
}

function productCard(product) {
  const node = document.createElement("div");
  node.className = "card";
  node.innerHTML = `<div><strong>${escapeHtml(product.name)}</strong><div class="meta">${escapeHtml(product.brand)} · ${escapeHtml(product.sku)} · rated ${product.rating}</div></div><div class="price">${escapeHtml(product.price)}</div>`;
  return node;
}

function receipt(title, detail) {
  const node = document.createElement("div");
  node.className = "receipt";
  node.innerHTML = `<strong>${escapeHtml(title)}</strong><div class="meta">${escapeHtml(detail)}</div>`;
  return node;
}

function renderLedger(events) {
  ledger.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("li");
    empty.className = "empty-ledger";
    empty.textContent = "No actions for this turn yet.";
    ledger.append(empty);
    return;
  }
  events.forEach((event) => {
    const item = document.createElement("li");
    const status = event.status || "noted";
    item.innerHTML = `
      <div class="event">
        <b>${escapeHtml(event.action)}</b>
        <span class="status ${escapeHtml(status)}">${escapeHtml(status)}</span>
      </div>
      <p class="why">${escapeHtml(event.actor_type || "system")} · ${escapeHtml(event.actor_id || "")}${event.reason ? " — " + escapeHtml(event.reason) : ""}</p>`;
    ledger.append(item);
  });
  ledger.scrollTop = ledger.scrollHeight;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
