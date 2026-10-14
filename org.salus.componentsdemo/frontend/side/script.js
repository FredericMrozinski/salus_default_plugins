import { Salus, Payload } from "../salus_sdk.js";

const salus = Salus.connect();
const logEl = document.getElementById("log");
const log = (line) => (logEl.textContent += line + "\n");

document.getElementById("who").textContent = `Side: I am "${salus.component}" (frontend ${salus.frontendId})`;
log(`params: ${JSON.stringify(salus.params)}`);

// The component that opened this one. Messages between components of one plugin stay in the browser.
const main = salus.parent;
if (main) {
  main.channel("note", (message) => {
    log(`note from main: ${message.payload.asStr()}`);
    main.send("reply", Payload.str(`got "${message.payload.asStr()}"`));
  });
}
salus.channel("hello", (message) => log(`backend: ${message.payload.asStr()}`));
