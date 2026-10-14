import { Salus, Payload } from "../salus_sdk.js";

const salus = Salus.connect();
const logEl = document.getElementById("log");
const log = (line) => (logEl.textContent += line + "\n");
const click = (id, handler) => document.getElementById(id).addEventListener("click", async () => {
  try { await handler(); } catch (error) { log(`error: ${error.message}`); }
});

document.getElementById("who").textContent = `Components Demo: I am "${salus.component}" (frontend ${salus.frontendId})`;
log(`params: ${JSON.stringify(salus.params)}`);

salus.channel("hello", (message) => log(`backend: ${message.payload.asStr()}`));
// Messages from components that were not opened through here (siblings) arrive unclaimed.
salus.onUnclaimed((message) => log(`message ${message.channel}: ${message.payload.asStr()}`));

let side = null;
let replyChannel = null;

click("open-side", async () => {
  side = await salus.openComponent("side", { reuse: true, params: { from: "main" } });
  if (!replyChannel) replyChannel = side.channel("reply", (message) => log(`side replied: ${message.payload.asStr()}`));
  log(`side is frontend ${side.id} (component ${side.component})`);
});

click("send", async () => {
  if (!side) throw new Error("open the side component first");
  side.send("note", Payload.str(document.getElementById("note").value));
});

click("list", async () => {
  const peers = await salus.components();
  log(`open components: ${JSON.stringify(peers.map((p) => [p.id, p.component]))}`);
});

click("open-file", () => salus.openFile("/tmp/example.demo"));
click("open-unknown", () => salus.openFile("/tmp/file.unknown"));
