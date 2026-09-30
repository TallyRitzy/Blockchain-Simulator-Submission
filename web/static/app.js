"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="simulation-token"]').content;
let state = null;
let busy = false;
let refreshing = false;
let noticeUntil = 0;
const short = key => key ? key.replace(/-----[^-]+-----|\s/g, "").slice(-12) : "—";
const number = value => new Intl.NumberFormat(undefined, {maximumFractionDigits: 6}).format(value);
const nameFor = key => {
  if (!state) return short(key);
  if (key === state.public_key) return state.name + " (you)";
  const peer = state.peers.find(p => p.public_key === key);
  return peer ? peer.name : short(key);
};
function notify(message, error = false) {
  $("notice").hidden = false;
  $("notice").className = error ? "error" : "";
  $("notice").textContent = message;
  noticeUntil = Date.now() + 12000;
}
function cell(row, text, className = "") {
  const td = document.createElement("td");
  td.textContent = text;
  td.className = className;
  row.append(td);
}
function emptyRow(target, text, columns) {
  const row = document.createElement("tr");
  cell(row, text, "empty");
  row.firstChild.colSpan = columns;
  target.append(row);
}
function options(select, rows, fallback) {
  const chosen = select.value;
  select.replaceChildren();
  const blank = document.createElement("option");
  blank.value = ""; blank.textContent = fallback; select.append(blank);
  rows.forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value; option.textContent = label; select.append(option);
  });
  if (rows.some(([value]) => value === chosen)) select.value = chosen;
}
function drawNetwork() {
  const graph = $("networkGraph");
  graph.replaceChildren();
  const ns = "http://www.w3.org/2000/svg";
  const create = (tag, attrs) => {
    const el = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    graph.append(el); return el;
  };
  const nodes = state.peers.slice(0, 8);
  nodes.forEach((p, i) => {
    const angle = 2 * Math.PI * i / Math.max(nodes.length, 1) - Math.PI / 2;
    const x = 260 + 180 * Math.cos(angle), y = 110 + 68 * Math.sin(angle);
    const line = create("line", {x1:260, y1:110, x2:x, y2:y});
    if (!p.connected) line.setAttribute("stroke-dasharray", "5 5");
    create("circle", {cx:x, cy:y, r:15});
    const label = create("text", {x, y:y + 32});
    label.textContent = p.name.slice(0, 20);
  });
  create("circle", {cx:260, cy:110, r:24, class:"self"});
  const label = create("text", {x:260, y:114, class:"self-label"}); label.textContent = "YOU";
  if (!nodes.length) {
    const text = create("text", {x:260, y:170});
    text.textContent = "Start another node in this room";
  }
}
function render() {
  $("nodeTitle").textContent = state.name;
  $("nodeDetails").textContent = state.host + ":" + state.port + (state.malicious ? " · Malicious node" : " · Honest node");
  $("room").textContent = state.room;
  $("discovery").textContent = state.signalling_connected ? "Directory connected" : "Directory offline / bootstrap mode";
  $("connectionStatus").textContent = state.ready ? "Node online" : "Synchronising blockchain";
  $("statusDot").className = state.ready ? "dot ready" : "dot";
  $("available").textContent = number(state.available);
  $("balance").textContent = number(state.balance);
  $("locked").textContent = number(state.stake);
  $("height").textContent = Math.max(0, state.height);
  $("weight").textContent = "Chain weight " + number(state.weight);
  $("connections").textContent = state.connections;
  $("peerCount").textContent = state.peers.length + " discovered · " + state.validators.length + " validators";
  $("countdown").textContent = Math.ceil(state.next_slot_seconds) + "s";
  $("slot").textContent = "Slot " + state.slot + " · " + state.epoch_seconds + "s rounds";
  $("publicKey").value = state.public_key;
  $("validatorState").textContent = !state.staker ? "Observer" : state.stake ? "Active · " + state.stake + " coins" : "No confirmed stake";
  $("networkId").textContent = "Network " + state.network.slice(0, 12);
  $("networkId").title = state.network;
  $("telemetry").textContent = state.telemetry;
  options($("receiver"), state.peers.map(p => [p.public_key, p.name + " · port " + p.port]), "Choose a receiver");
  options($("contractId"), state.contracts.map(id => [id, id.slice(0, 20) + "…"]), "Choose a confirmed contract");
  const peers = $("peers"); peers.replaceChildren();
  if (!state.peers.length) emptyRow(peers, "No other nodes discovered", 4);
  state.peers.forEach(peer => {
    const row = document.createElement("tr");
    cell(row, peer.name); cell(row, peer.host + ":" + peer.port, "mono");
    cell(row, peer.connected ? "Connected" : "Discovered", peer.connected ? "link-up" : "link-down");
    const stake = state.validators.find(v => v.public_key === peer.public_key);
    cell(row, stake ? number(stake.stake) : "0"); peers.append(row);
  });
  drawNetwork();
  const blocks = $("blocks"); blocks.replaceChildren();
  [...state.blocks].reverse().forEach(block => {
    const card = document.createElement("article"); card.className = "block";
    const title = document.createElement("h3"); title.textContent = block.height ? "Block " + block.height : "Genesis";
    const hash = document.createElement("div"); hash.className = "hash"; hash.textContent = block.hash.slice(0, 16) + "…"; hash.title = block.hash;
    card.append(title, hash);
    [nameFor(block.creator), "Slot " + block.slot + " · " + block.transactions + " transactions",
      block.stake + " coins staked" + (block.penalties ? " · " + block.penalties + " penalties" : "")].forEach(text => {
      const p = document.createElement("p"); p.textContent = text; card.append(p);
    });
    blocks.append(card);
  });
  $("pendingCount").textContent = state.pending.length;
  const pending = $("pending"); pending.replaceChildren();
  if (!state.pending.length) emptyRow(pending, "No pending transactions", 3);
  state.pending.forEach(tx => {
    const row = document.createElement("tr");
    const special = ["stake", "deploy", "invoke"].includes(tx.receiver);
    cell(row, special ? tx.receiver : "transfer");
    cell(row, nameFor(tx.sender) + " → " + (special ? tx.receiver : nameFor(tx.receiver)));
    cell(row, number(Array.isArray(tx.payload) ? tx.payload.at(-1) : tx.payload)); pending.append(row);
  });
  $("rejected").textContent = state.rejected_messages + " rejected";
  $("penalties").textContent = state.pending_evidence + " pending evidence · " + state.confirmed_penalties + " confirmed penalties";
  const events = $("events"); events.replaceChildren();
  [...state.events].reverse().slice(0, 20).forEach(event => {
    const item = document.createElement("li"); item.className = event.kind;
    const time = document.createElement("time"); time.textContent = event.time;
    const text = document.createElement("span"); text.textContent = event.message;
    item.append(time, text); events.append(item);
  });
  $("attackPanel").hidden = !state.malicious;
  document.querySelectorAll('form button, #attack').forEach(b => b.disabled = busy || !state.ready);
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error("Node state is temporarily unavailable");
    state = await response.json(); render();
    if (Date.now() > noticeUntil) $("notice").hidden = true;
  } catch (error) {
    $("connectionStatus").textContent = "Node unreachable"; $("statusDot").className = "dot";
  } finally { refreshing = false; }
}
async function post(url, data) {
  busy = true; if (state) render();
  try {
    const response = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json", "X-Simulation-Token":token}, body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Command failed");
    notify("Submitted " + result.id.slice(0, 12) + ". Waiting for network confirmation.");
    await refresh();
  } catch (error) { notify(error.message, true); }
  finally { busy = false; if (state) render(); }
}
$("transferForm").addEventListener("submit", event => {
  event.preventDefault(); post("/api/transactions", {receiver:$("receiver").value, amount:Number($("amount").value)});
});
$("stakeForm").addEventListener("submit", event => {
  event.preventDefault(); post("/api/stakes", {amount:Number($("stakeAmount").value)});
});
$("deployForm").addEventListener("submit", event => {
  event.preventDefault(); post("/api/contracts/deploy", {code:$("contractCode").value});
});
$("invokeForm").addEventListener("submit", event => {
  event.preventDefault();
  try { post("/api/contracts/invoke", {contract_id:$("contractId").value, function:$("functionName").value, args:JSON.parse($("contractArgs").value)}); }
  catch (error) { notify("Arguments must be a JSON array, for example [1].", true); }
});
$("attack").addEventListener("click", () => post("/api/attack", {receiver:$("receiver").value}));
$("copyKey").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("publicKey").value); notify("Public key copied."); }
  catch (error) { notify("Select the public key and copy it manually.", true); }
});
refresh();
setInterval(refresh, 2000);

