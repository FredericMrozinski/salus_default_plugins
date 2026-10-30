// A folder (or file) picker: the server's folders as a tree that loads a folder when it is opened.
import { api, h, message } from "./common.js";

/** Resolves with the chosen folder's path, or null when cancelled. With `files` (a list of endings such as [".geojson"])
 *  matching files are listed too, and a file can be chosen as well. */
export function pickFolder(title, start, { files = null } = {}) {
  return new Promise((resolve) => {
    const nodes = new Map(); // path -> {folders, open, loaded, error}
    let chosen = null;
    const tree = h("div", { class: "tree" });
    const pathLine = h("input", { type: "text", class: "mono", placeholder: "or type a path and press Enter", spellcheck: false });
    const error = h("div", { class: "error" });
    const choose = h("button", { class: "primary", disabled: true }, "Choose");
    const join = (dir, name) => `${dir.replace(/\/$/, "")}/${name}`;

    async function load(path) {
      const node = nodes.get(path) ?? {};
      nodes.set(path, node);
      if (node.loaded) return node;
      try {
        const listing = await api("GET", "/fs", { query: { path } });
        const wanted = (name) => files && files.some((ending) => name.toLowerCase().endsWith(ending));
        Object.assign(node, { folders: listing.folders.map((f) => f.name), files: listing.files.map((f) => f.name).filter(wanted), loaded: true, error: null });
      } catch (e) {
        Object.assign(node, { folders: [], files: [], loaded: true, error: message(e) });
      }
      return node;
    }

    async function reveal(path) {
      let current = "/";
      for (const part of [null, ...path.split("/").filter(Boolean)]) {
        if (part !== null) current = join(current, part);
        const node = await load(current);
        if (node.error) break;
        node.open = true;
      }
      select(path);
      const row = [...tree.children].find((r) => r.dataset.path === path);
      if (row) tree.scrollTop = Math.max(0, row.offsetTop - tree.offsetTop - 60);
    }

    async function toggle(path) {
      const node = await load(path);
      node.open = !node.open;
      draw();
    }

    function select(path) {
      chosen = path;
      pathLine.value = path;
      choose.disabled = !path;
      draw();
    }

    function rows(path, depth) {
      const node = nodes.get(path);
      if (!node?.open) return [];
      const out = node.error ? [h("div", { class: "row error", style: `padding-left:${8 + depth * 16}px` }, node.error)] : [];
      for (const name of node.folders ?? []) {
        const child = join(path, name);
        const open = nodes.get(child)?.open;
        out.push(h("div", { class: "row" + (child === chosen ? " chosen" : ""), "data-path": child, style: `padding-left:${8 + depth * 16}px`, onclick: () => select(child), ondblclick: () => toggle(child) },
          h("span", { class: "chevron", onclick: (e) => (e.stopPropagation(), toggle(child)) }, open ? "▾" : "▸"), h("span", { class: "folder-icon" }, "📁"), name),
          ...rows(child, depth + 1));
      }
      for (const name of node.files ?? []) {
        const child = join(path, name);
        out.push(h("div", { class: "row file" + (child === chosen ? " chosen" : ""), "data-path": child, style: `padding-left:${8 + depth * 16}px`, onclick: () => select(child), ondblclick: () => close(child) },
          h("span", { class: "chevron" }), h("span", { class: "folder-icon" }, "📄"), name));
      }
      return out;
    }

    function draw() {
      const root = nodes.get("/");
      tree.replaceChildren(h("div", { class: "row" + (chosen === "/" ? " chosen" : ""), "data-path": "/", onclick: () => select("/") },
        h("span", { class: "chevron", onclick: (e) => (e.stopPropagation(), toggle("/")) }, root?.open ? "▾" : "▸"), h("span", { class: "folder-icon" }, "📁"), "/"), ...rows("/", 1));
    }

    pathLine.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter") return;
      const value = pathLine.value.trim();
      if (!value.startsWith("/") && !value.startsWith("~")) return void (error.textContent = "Enter the full path (it starts with / or ~).");
      error.textContent = "";
      const listing = await api("GET", "/fs", { query: { path: value } }).catch(() => null);
      const isFolder = listing?.path === value.replace(/\/$/, "") || value === "/";
      await reveal(isFolder ? value : (listing?.path ?? value));
      if (!isFolder && files && listing) select(value); // a file: show its folder, choose the file
    });

    const close = (result) => {
      dismiss();
      resolve(result);
    };
    choose.addEventListener("click", () => close(chosen));
    const dismiss = modalTree(title, [pathLine, tree, h("div", { class: "faint" }, files ? "Click a folder or a file to choose it; the arrows open and close folders." : "Click a folder to choose it; the arrows open and close folders."), error],
      [h("button", { onclick: () => close(null) }, "Cancel"), choose]);
    (async () => {
      let first = start;
      if (!first) first = (await api("GET", "/fs", { query: { path: "~" } }).catch(() => ({ path: "/" }))).path;
      await reveal(first);
    })();
  });
}

function modalTree(title, content, buttons) {
  const backdrop = h("div", { class: "backdrop top" },
    h("div", { class: "dialog wide" }, h("header", {}, title), h("div", { class: "content" }, content), h("footer", {}, buttons)));
  document.body.append(backdrop);
  return () => backdrop.remove();
}
