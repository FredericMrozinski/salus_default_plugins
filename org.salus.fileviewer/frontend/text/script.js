import { Salus } from "../salus_sdk.js";

// Text files (source code, GeoJSON, YAML, logs, ...): the backend reads and decodes the file (backend/texts.py), and
// highlight.js (frontend/vendor, BSD-3-Clause) colors it. The language comes from the file type, or highlight.js guesses
// it from the text; the list at the top changes it. JSON can be shown formatted. The file itself is never changed.
const salus = Salus.connect();
const file = salus.params?.file; // set by Salus when it opens this viewer for a file (see file_viewer_for in the manifest)
const hljs = window.hljs;

const $ = (id) => document.getElementById(id);
const HIGHLIGHT_LIMIT = 2 * 1024 * 1024; // characters; above that the text is shown plain (coloring would stall the page)
const GUESS_SAMPLE = 64 * 1024; // highlight.js guesses the language from the start of the text

// File types and their highlight.js language. Types not listed are guessed from the text.
const BY_EXTENSION = {
  json: "json", geojson: "json", topojson: "json", ipynb: "json", jsonc: "json", json5: "json",
  txt: "plaintext", text: "plaintext", log: "plaintext", out: "plaintext",
  md: "markdown", markdown: "markdown",
  xml: "xml", kml: "xml", gpx: "xml", svg: "xml", html: "xml", htm: "xml", xsd: "xml",
  yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", cfg: "ini", conf: "ini", properties: "ini", env: "bash",
  py: "python", pyi: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", tsx: "typescript", css: "css", scss: "scss", less: "less",
  sh: "bash", bash: "bash", zsh: "bash", r: "r", sql: "sql", c: "c", h: "c", cpp: "cpp", cc: "cpp", hpp: "cpp",
  java: "java", kt: "kotlin", rs: "rust", go: "go", rb: "ruby", pl: "perl", lua: "lua", php: "php", swift: "swift",
  cs: "csharp", groovy: "java", diff: "diff", patch: "diff", graphql: "graphql", mk: "makefile",
};
const NAMES = { plaintext: "Plain text", javascript: "JavaScript", typescript: "TypeScript", json: "JSON", xml: "XML / HTML", yaml: "YAML",
  ini: "INI / TOML", bash: "Shell", cpp: "C++", csharp: "C#", objectivec: "Objective-C", php: "PHP", sql: "SQL", css: "CSS", scss: "SCSS",
  graphql: "GraphQL", "php-template": "PHP template", "python-repl": "Python console", vbnet: "VB.NET", wasm: "WebAssembly" };
const nameOf = (id) => NAMES[id] ?? id.charAt(0).toUpperCase() + id.slice(1);

const state = { doc: null, text: "", formatted: null, language: "auto", guessed: null };

function fail(text) {
  $("message").textContent = text;
  $("message").className = "message error";
  $("message").hidden = false;
  $("view").hidden = true;
}

function bytes(n) {
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) (n /= 1024), i++;
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
}

// --------------------------------------------------------------------------- theme

// Salus adds its theme stylesheet after the page has loaded, so the background is looked at again when stylesheets come
// or go. Only children are watched: switching the themes below changes attributes, which must not start it again.
function matchTheme() {
  const [r, g, b, a = 1] = (getComputedStyle(document.body).backgroundColor.match(/\d+(\.\d+)?/g) ?? [255, 255, 255]).map(Number);
  const dark = a > 0 && 0.2126 * r + 0.7152 * g + 0.0722 * b < 128; // no background yet (transparent): light
  if ($("light-theme").disabled !== dark) $("light-theme").disabled = dark;
  if ($("dark-theme").disabled !== !dark) $("dark-theme").disabled = !dark;
}
new MutationObserver(matchTheme).observe(document.head, { childList: true });
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", matchTheme);
matchTheme();

// --------------------------------------------------------------------------- showing the text

if (!file) fail("No file was given.");
else if (!hljs) fail("The syntax highlighter (vendor/highlight.min.js) is missing.");
else start().catch((error) => fail(`Could not open the file: ${error.body || error.message}`));

async function start() {
  const doc = await salus.http.json("GET", "/text/open", { query: { file } });
  state.doc = doc;
  state.text = doc.text.replace(/\r\n?/g, "\n");
  const extension = doc.name.includes(".") ? doc.name.split(".").pop().toLowerCase() : "";
  const known = BY_EXTENSION[extension];
  state.language = known && hljs.getLanguage(known) ? known : "auto";

  $("file").textContent = doc.name;
  $("file").title = file;
  const lines = state.text ? state.text.split("\n").length - (state.text.endsWith("\n") ? 1 : 0) : 0;
  $("info").textContent = `${lines.toLocaleString()} line${lines === 1 ? "" : "s"} · ${bytes(doc.size)} · ${doc.encoding}`;
  if (doc.truncated) notice(`The file is ${bytes(doc.size)}; the first ${bytes(doc.shown_bytes)} are shown.`);

  const select = $("language");
  select.append(new Option("Detect automatically", "auto"));
  for (const id of hljs.listLanguages().sort((a, b) => nameOf(a).localeCompare(nameOf(b)))) select.append(new Option(nameOf(id), id));
  select.value = state.language;
  select.addEventListener("change", () => ((state.language = select.value), show()));

  // Minified JSON (GeoJSON often is) is one huge line: formatted, it can be read.
  const firstBreak = state.text.indexOf("\n");
  const longLine = (firstBreak === -1 ? state.text.length : firstBreak) > 2000;
  $("format").checked = longLine;
  $("format").addEventListener("change", show);
  $("wrap").addEventListener("change", () => $("view").classList.toggle("wrap", $("wrap").checked));

  $("message").hidden = true;
  $("view").hidden = false;
  show();
}

function notice(text) {
  $("notice").textContent = text;
  $("notice").hidden = false;
}

// JSON, formatted once; null if the text is not valid JSON (then the toggle hides). Arrays of plain values stay on one
// line, so a GeoJSON point reads [137, 582] instead of taking four lines.
function formattedJson() {
  if (state.formatted === null && !state.doc.truncated) {
    try {
      state.formatted = JSON.stringify(JSON.parse(state.text), null, 2)
        .replace(/\[\n\s*([^[\]{}]*?)\n\s*\]/g, (_, inner) => `[${inner.split(/,\n\s*/).join(", ")}]`);
    } catch {
      state.formatted = false;
    }
  }
  return state.formatted || null;
}

function show() {
  let language = state.language;
  if (language === "auto") {
    state.guessed ??= state.text.length ? hljs.highlightAuto(state.text.slice(0, GUESS_SAMPLE)).language ?? "plaintext" : "plaintext";
    language = state.guessed;
    $("language").options[0].text = `Detect automatically (${nameOf(language)})`;
  }
  const isJson = language === "json";
  const pretty = isJson ? formattedJson() : null;
  $("formatToggle").hidden = !pretty;
  const text = pretty && $("format").checked ? pretty : state.text;

  const code = $("code");
  code.className = `hljs language-${language}`;
  if (text.length > HIGHLIGHT_LIMIT || language === "plaintext") {
    code.textContent = text;
    if (text.length > HIGHLIGHT_LIMIT && language !== "plaintext") notice(`The text is too long to be colored (more than ${bytes(HIGHLIGHT_LIMIT)} characters); it is shown plain.`);
  } else {
    code.innerHTML = hljs.highlight(text, { language, ignoreIllegals: true }).value; // highlight.js escapes the text
  }
  const count = text ? text.split("\n").length - (text.endsWith("\n") ? 1 : 0) : 1;
  $("gutter").textContent = Array.from({ length: Math.max(count, 1) }, (_, i) => i + 1).join("\n");
}
