import { Salus, Payload } from "../salus_sdk.js";

const salus = Salus.connect();
const log = (line) => (document.getElementById("log").textContent += line + "\n");

document.getElementById("who").textContent = `Viewer: I am "${salus.component}" (frontend ${salus.frontendId})`;
document.getElementById("file").textContent = salus.params?.file ?? "(none)";

// This viewer was opened by Salus for a file, not by main; it still finds its sibling components.
document.getElementById("ping").addEventListener("click", async () => {
  const [main] = await salus.components("main");
  if (!main) return log("main is not open");
  main.send("ping", Payload.str(`ping from ${salus.component} for ${salus.params?.file}`));
  log(`pinged main (frontend ${main.id})`);
});
