import { Salus } from "./salus_sdk.js";
import { Terminal } from "./vendor/xterm.mjs";
import { FitAddon } from "./vendor/addon-fit.mjs";

const salus = Salus.connect();
const container = document.getElementById("terminal");

const term = new Terminal({
  fontSize: 13,
  fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--salus-font-mono") || "monospace",
  cursorBlink: true,
  scrollback: 5000,
  allowProposedApi: true,
});
const fit = new FitAddon();
term.loadAddon(fit);
term.open(container);

/** The terminal's colors follow the Salus theme (and change with it). */
function applyTheme() {
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  term.options.theme = {
    background: color("--salus-bg"),
    foreground: color("--salus-fg"),
    cursor: color("--salus-accent"),
    cursorAccent: color("--salus-bg"),
    selectionBackground: /^#[0-9a-f]{6}$/i.test(color("--salus-accent")) ? `${color("--salus-accent")}66` : undefined,
  };
}
applyTheme();
// Salus adds its stylesheet and sets the theme a moment after the page loaded, and again when the user picks another theme.
new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

// ---- the shell
// The channels and their handlers come first and nothing is awaited before them: the backend may report "ready" at any moment.
const input = salus.channel("input");
const output = salus.channel("output");
const control = salus.channel("control");
const sendControl = (message) => control.send(JSON.stringify(message));
let running = false;

/**
 * Asks the backend for a shell. A message to a backend that is still starting is lost, so this is asked again when the
 * backend says "ready"; a backend that has a shell already ignores it.
 */
function start() {
  fit.fit();
  running = true;
  sendControl({ type: "start", cols: term.cols, rows: term.rows });
}

output.onMessage((message) => term.write(message.payload.asBytes()));

control.onMessage((message) => {
  const event = message.payload.asJson();
  if (event.type === "ready") {
    start();
  } else if (event.type === "exit") {
    running = false;
    term.write(`\r\n\x1b[2m[The shell ended (exit code ${event.code}). Press Enter to start a new one.]\x1b[0m\r\n`);
  } else if (event.type === "error") {
    running = false;
    term.write(`\r\n\x1b[31m${event.message}\x1b[0m\r\n\x1b[2m[Press Enter to try again.]\x1b[0m\r\n`);
  }
});

term.onData((data) => {
  if (running) input.send(data);
  else if (data === "\r") start();
});
term.onBinary((data) => {
  if (running) input.send(Uint8Array.from(data, (c) => c.charCodeAt(0)));
});

// ---- size
let lastSize = "";
function refit() {
  if (container.clientWidth === 0 || container.clientHeight === 0) return;
  fit.fit();
  const size = `${term.cols}x${term.rows}`;
  if (running && size !== lastSize) sendControl({ type: "resize", cols: term.cols, rows: term.rows });
  lastSize = size;
}
new ResizeObserver(refit).observe(container);

salus.onShown(() => {
  refit();
  term.focus();
});

start();

// The font size setting; a setting that cannot be read must not keep the terminal from working.
salus.config
  .get("font_size", 13)
  .then((size) => {
    term.options.fontSize = Number(size) || 13;
    refit();
  })
  .catch(() => {});
