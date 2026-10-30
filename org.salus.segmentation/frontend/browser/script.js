// The dataset browser (left panel): chooses the project and lists its images. It is the plugin's entry component and
// opens the other three: the viewer (center), the tools (bottom) and the classes (right).
import { api, dirName, fileName, followState, h, message, modal, onEvent, salus, setState } from "../shared/common.js";
import { pickFolder } from "../shared/picker.js";

const app = document.getElementById("app");
let images = []; // [{path, shapes}]
let filter = "";
let loadedFor = null; // project id the image list belongs to
let error = "";

const shared = followState(async (state) => {
  if (state.project && loadedFor !== state.project.id) await loadImages(false, state.project.id);
  if (!state.project) (images = []), (loadedFor = null);
  render(state);
});

onEvent((event) => {
  if (event.type !== "annotations") return;
  const image = images.find((i) => i.path === event.image);
  if (image) {
    image.shapes = event.shapes;
    render(shared.get());
  }
});

async function loadImages(rescan, projectId = shared.get()?.project?.id) {
  try {
    images = (await api("GET", "/images", { query: { rescan: rescan ? "1" : undefined } })).images;
    loadedFor = projectId;
    error = "";
  } catch (e) {
    error = message(e);
  }
}

// The other components of the workspace. `reuse` focuses them if they are open already.
async function openWorkspace() {
  for (const name of ["viewer", "tools", "classes"]) {
    try {
      await salus.openComponent(name, { reuse: true });
    } catch (e) {
      console.error(`could not open ${name}`, e);
    }
  }
}

function render(state) {
  if (!state) return;
  const project = state.project;
  const select = h("select", {
    onchange: async (e) => {
      if (!e.target.value) return;
      await guard(() => api("POST", `/projects/${e.target.value}/use`));
    },
  },
    !project && h("option", { value: "" }, state.projects.length ? "Choose a project…" : "No projects yet"),
    state.projects.map((p) => h("option", { value: p.id, selected: project?.id === p.id, disabled: p.missing }, p.missing ? `${p.name} (folder missing)` : p.name)));

  const head = h("div", { class: "project" },
    h("div", { class: "line" }, select,
      h("button", { title: "Create a new annotation project", onclick: newProjectDialog }, "New"),
      h("button", { title: "Open a project from its save folder", onclick: openProjectDialog }, "Open"),
      project && h("button", { title: "Remove this project from the list (its files stay where they are)", onclick: () => forget(project) }, "×")),
    project && h("div", { class: "line" },
      h("button", { class: "grow", title: "Import annotations exported from QuPath (GeoJSON)", onclick: () => importDialog(state) }, "Import from QuPath"),
      h("button", { class: "grow", title: "Export the annotations as pixel maps (pyramidal TIFF)", onclick: () => salus.openComponent("export", { reuse: true }).catch((e) => console.error(e)) }, "Export")),
    project && h("div", { class: "paths", title: `Images: ${project.root}\nAnnotations: ${project.save}\nFile types: ${project.extensions.join(", ")}` },
      `${project.root} · ${project.extensions.join(", ")}`),
    project?.root_missing && h("div", { class: "warn" }, "The image folder does not exist (any more)."));

  if (!project) {
    app.replaceChildren(head, h("div", { class: "welcome" },
      h("p", { class: "faint" }, "An annotation project lists the images of a folder and keeps their annotations in a separate save folder. Create one, or open one from its save folder."),
      h("button", { class: "primary", onclick: newProjectDialog }, "New project…"),
      error && h("div", { class: "error" }, error)));
    return;
  }

  const needle = filter.trim().toLowerCase();
  const shown = needle ? images.filter((i) => i.path.toLowerCase().includes(needle)) : images;
  const search = h("input", { type: "search", placeholder: "Filter images…", value: filter, oninput: (e) => { filter = e.target.value; render(shared.get()); } });
  const list = h("div", { class: "list" },
    shown.map((image) => h("div", {
      class: "image" + (image.path === state.image ? " current" : ""),
      title: image.path,
      onclick: () => openImage(image.path),
    },
      h("div", { class: "text" }, h("div", { class: "name" }, fileName(image.path)), dirName(image.path) && h("div", { class: "dir" }, dirName(image.path))),
      h("span", { class: "badge" + (image.shapes ? " has" : ""), title: `${image.shapes} shapes` }, String(image.shapes)))),
    !shown.length && h("div", { class: "empty" }, images.length ? "No image matches." : `No ${project.extensions.join(" / ")} files in the image folder.`),
    error && h("div", { class: "empty error" }, error));
  const annotated = images.filter((i) => i.shapes).length;

  const focused = document.activeElement === app.querySelector(".search input");
  app.replaceChildren(head,
    h("div", { class: "search" }, search, h("button", { title: "Look for new or removed images", onclick: async () => { await loadImages(true); render(shared.get()); } }, "↻")),
    list,
    h("div", { class: "footer" }, `${images.length} images, ${annotated} annotated`));
  if (focused) {
    search.focus();
    search.setSelectionRange(search.value.length, search.value.length);
  }
}

async function openImage(path) {
  await guard(() => setState({ image: path }));
  // --------------------------------------------------------------------------- importing QuPath annotations

// Step 1 chooses the source: one GeoJSON file for the open image, or a folder whose files are matched to images by name.
// Step 2 shows what was found and maps every QuPath class to a project class (or leaves it out). The QuPath files are only read.
function importDialog(state) {
  let kind = state.image ? "file" : "folder";
  const path = h("input", { type: "text", class: "mono", placeholder: "/path/to/qupath/export", spellcheck: false });
  const problem = h("div", { class: "error" });
  const kinds = h("div", { class: "choices" });
  const drawKinds = () => kinds.replaceChildren(
    choice("kind", kind === "file", !state.image, () => ((kind = "file"), drawKinds()),
      "One file for the open image", state.image ? fileName(state.image) : "Open an image first"),
    choice("kind", kind === "folder", false, () => ((kind = "folder"), drawKinds()),
      "A folder of QuPath exports", "Each file is matched to the image with the same name, e.g. slide1.geojson to slide1.svs (subfolders count)."));
  drawKinds();
  const next = h("button", { class: "primary" }, "Next");
  next.addEventListener("click", async () => {
    next.disabled = true;
    const source = { path: path.value.trim(), image: kind === "file" ? state.image : undefined };
    try {
      const scan = await api("POST", "/import/scan", { json: source });
      close();
      mappingDialog(source, scan);
    } catch (e) {
      problem.textContent = message(e);
      next.disabled = false;
    }
  });
  const browse = h("button", { type: "button", onclick: async () => {
    const chosen = kind === "file"
      ? await pickFolder("Choose the QuPath GeoJSON file", path.value.trim() || undefined, { files: [".geojson", ".json"] })
      : await pickFolder("Choose the folder with the QuPath exports", path.value.trim() || undefined);
    if (chosen) path.value = chosen;
  } }, "Browse…");
  const close = modal("Import from QuPath", [
    h("p", { class: "faint" }, "Annotations exported from QuPath (File › Export objects as GeoJSON). The QuPath files are only read; the imported shapes become annotations of this project."),
    kinds,
    h("label", { class: "field" }, h("span", {}, "File or folder"), h("div", { class: "with-button" }, path, browse)),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), next]);
}

function choice(name, checked, disabled, onchange, title, detail) {
  return h("label", { class: "choice" + (disabled ? " disabled" : "") },
    h("input", { type: "radio", name, checked, disabled, onchange }),
    h("span", {}, h("strong", {}, title), detail && h("span", { class: "detail" }, detail)));
}

function mappingDialog(source, scan) {
  const classes = shared.get()?.project?.classes ?? [];
  const mapping = {};
  const existing = scan.files.filter((f) => f.existing > 0);
  let mode = existing.length ? null : "merge";
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary" }, "Import");
  const update = () => (go.disabled = !mode || !Object.values(mapping).some((v) => v !== null));

  const rows = scan.classes.map(({ name, count }) => {
    const same = classes.find((c) => c.name.toLowerCase() === name.toLowerCase());
    mapping[name] = same ? same.id : null;
    const select = h("select", { onchange: (e) => ((mapping[name] = e.target.value ? Number(e.target.value) : null), update()) },
      h("option", { value: "" }, "Don't import"),
      classes.map((c) => h("option", { value: c.id, selected: same?.id === c.id }, c.name)));
    return h("div", { class: "map-row" },
      h("div", { class: "map-name" }, name ? name : h("em", { class: "faint" }, "Unclassified"), h("span", { class: "faint" }, ` · ${count} shape${count === 1 ? "" : "s"}`)),
      h("div", { class: "map-target" }, h("span", { class: "faint" }, "→"), select));
  });

  const modeChoices = existing.length && h("div", { class: "choices" },
    h("div", {}, h("strong", {}, `${existing.length} of the images already ${existing.length === 1 ? "has" : "have"} annotations. What should happen to them?`)),
    choice("mode", false, false, () => ((mode = "merge"), update()), "Add the imported shapes", "The existing annotations stay."),
    choice("mode", false, false, () => ((mode = "replace"), update()), "Replace the existing annotations", "Images that get imported shapes lose their current annotations (undo brings them back)."));

  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const result = await api("POST", "/import/apply", { json: { ...source, mapping, mode } });
      close();
      modal("Import finished", h("p", {}, `${result.shapes} shape${result.shapes === 1 ? "" : "s"} imported into ${result.files} image${result.files === 1 ? "" : "s"}.`),
        [h("button", { class: "primary", onclick: (e) => e.target.closest(".backdrop").remove() }, "OK")]);
    } catch (e) {
      problem.textContent = message(e);
      update();
    }
  });

  const shapes = scan.files.reduce((n, f) => n + f.shapes, 0);
  const close = modal("Match the QuPath classes", [
    h("div", {}, `${scan.files.length} file${scan.files.length === 1 ? "" : "s"} with ${shapes} annotation${shapes === 1 ? "" : "s"} for ${scan.files.length} image${scan.files.length === 1 ? "" : "s"}.`,
      scan.skipped ? h("span", { class: "faint" }, ` ${scan.skipped} other objects (detections, points, lines) are left out.`) : ""),
    scan.files.length > 1 && h("details", {}, h("summary", {}, "Files and images"),
      h("ul", { class: "files" }, scan.files.map((f) => h("li", {}, `${fileName(f.file)} → ${f.image}`, h("span", { class: "faint" }, ` (${f.shapes})`))))),
    scan.left_out.length > 0 && h("details", {}, h("summary", { class: "warn" }, `${scan.left_out.length} file${scan.left_out.length === 1 ? " is" : "s are"} left out`),
      h("ul", { class: "files" }, scan.left_out.map((f) => h("li", {}, fileName(f.file), h("span", { class: "faint" }, ` (${f.reason})`))))),
    scan.classes.length
      ? h("div", { class: "mapping" }, h("div", { class: "map-head" }, "QuPath class → class in this project"), rows)
      : h("p", { class: "warn" }, "The files contain no annotations that can be imported."),
    h("p", { class: "faint" }, "Classes set to “Don't import” are left out. A class that is missing here can be added in the class editor; then start the import again."),
    modeChoices,
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), go]);
  update();
}

openWorkspace();
}

async function guard(action) {
  try {
    return await action();
  } catch (e) {
    error = message(e);
    render(shared.get());
    return undefined;
  }
}

async function forget(project) {
  const close = modal("Remove the project from the list?",
    h("p", {}, `“${project.name}” is only removed from this list. Its project file and annotations stay in ${project.save}; you can open it again later.`),
    [h("button", { onclick: () => close() }, "Cancel"), h("button", { class: "primary", onclick: async () => { close(); await guard(() => api("POST", `/projects/${project.id}/forget`)); } }, "Remove")]);
}

function folderField(label, input, hint) {
  return h("label", { class: "field" }, h("span", {}, label),
    h("div", { class: "with-button" }, input, h("button", { type: "button", onclick: async () => {
      const chosen = await pickFolder(`Choose the ${label.toLowerCase()}`, input.value.trim() || undefined);
      if (chosen) input.value = chosen;
    } }, "Browse…")), hint && h("span", {}, hint));
}

function newProjectDialog() {
  const name = h("input", { type: "text", placeholder: "e.g. Tumor segmentation" });
  const root = h("input", { type: "text", class: "mono", placeholder: "/path/to/the/images", spellcheck: false });
  const extensions = h("input", { type: "text", class: "mono", value: ".svs", spellcheck: false });
  const save = h("input", { type: "text", class: "mono", placeholder: "/path/to/the/annotations", spellcheck: false });
  const problem = h("div", { class: "error" });
  const create = h("button", { class: "primary" }, "Create");
  create.addEventListener("click", async () => {
    create.disabled = true;
    try {
      await api("POST", "/projects", { json: { name: name.value, root: root.value.trim(), extensions: extensions.value, save: save.value.trim() } });
      close();
      // --------------------------------------------------------------------------- importing QuPath annotations

// Step 1 chooses the source: one GeoJSON file for the open image, or a folder whose files are matched to images by name.
// Step 2 shows what was found and maps every QuPath class to a project class (or leaves it out). The QuPath files are only read.
function importDialog(state) {
  let kind = state.image ? "file" : "folder";
  const path = h("input", { type: "text", class: "mono", placeholder: "/path/to/qupath/export", spellcheck: false });
  const problem = h("div", { class: "error" });
  const kinds = h("div", { class: "choices" });
  const drawKinds = () => kinds.replaceChildren(
    choice("kind", kind === "file", !state.image, () => ((kind = "file"), drawKinds()),
      "One file for the open image", state.image ? fileName(state.image) : "Open an image first"),
    choice("kind", kind === "folder", false, () => ((kind = "folder"), drawKinds()),
      "A folder of QuPath exports", "Each file is matched to the image with the same name, e.g. slide1.geojson to slide1.svs (subfolders count)."));
  drawKinds();
  const next = h("button", { class: "primary" }, "Next");
  next.addEventListener("click", async () => {
    next.disabled = true;
    const source = { path: path.value.trim(), image: kind === "file" ? state.image : undefined };
    try {
      const scan = await api("POST", "/import/scan", { json: source });
      close();
      mappingDialog(source, scan);
    } catch (e) {
      problem.textContent = message(e);
      next.disabled = false;
    }
  });
  const browse = h("button", { type: "button", onclick: async () => {
    const chosen = kind === "file"
      ? await pickFolder("Choose the QuPath GeoJSON file", path.value.trim() || undefined, { files: [".geojson", ".json"] })
      : await pickFolder("Choose the folder with the QuPath exports", path.value.trim() || undefined);
    if (chosen) path.value = chosen;
  } }, "Browse…");
  const close = modal("Import from QuPath", [
    h("p", { class: "faint" }, "Annotations exported from QuPath (File › Export objects as GeoJSON). The QuPath files are only read; the imported shapes become annotations of this project."),
    kinds,
    h("label", { class: "field" }, h("span", {}, "File or folder"), h("div", { class: "with-button" }, path, browse)),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), next]);
}

function choice(name, checked, disabled, onchange, title, detail) {
  return h("label", { class: "choice" + (disabled ? " disabled" : "") },
    h("input", { type: "radio", name, checked, disabled, onchange }),
    h("span", {}, h("strong", {}, title), detail && h("span", { class: "detail" }, detail)));
}

function mappingDialog(source, scan) {
  const classes = shared.get()?.project?.classes ?? [];
  const mapping = {};
  const existing = scan.files.filter((f) => f.existing > 0);
  let mode = existing.length ? null : "merge";
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary" }, "Import");
  const update = () => (go.disabled = !mode || !Object.values(mapping).some((v) => v !== null));

  const rows = scan.classes.map(({ name, count }) => {
    const same = classes.find((c) => c.name.toLowerCase() === name.toLowerCase());
    mapping[name] = same ? same.id : null;
    const select = h("select", { onchange: (e) => ((mapping[name] = e.target.value ? Number(e.target.value) : null), update()) },
      h("option", { value: "" }, "Don't import"),
      classes.map((c) => h("option", { value: c.id, selected: same?.id === c.id }, c.name)));
    return h("div", { class: "map-row" },
      h("div", { class: "map-name" }, name ? name : h("em", { class: "faint" }, "Unclassified"), h("span", { class: "faint" }, ` · ${count} shape${count === 1 ? "" : "s"}`)),
      h("div", { class: "map-target" }, h("span", { class: "faint" }, "→"), select));
  });

  const modeChoices = existing.length && h("div", { class: "choices" },
    h("div", {}, h("strong", {}, `${existing.length} of the images already ${existing.length === 1 ? "has" : "have"} annotations. What should happen to them?`)),
    choice("mode", false, false, () => ((mode = "merge"), update()), "Add the imported shapes", "The existing annotations stay."),
    choice("mode", false, false, () => ((mode = "replace"), update()), "Replace the existing annotations", "Images that get imported shapes lose their current annotations (undo brings them back)."));

  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const result = await api("POST", "/import/apply", { json: { ...source, mapping, mode } });
      close();
      modal("Import finished", h("p", {}, `${result.shapes} shape${result.shapes === 1 ? "" : "s"} imported into ${result.files} image${result.files === 1 ? "" : "s"}.`),
        [h("button", { class: "primary", onclick: (e) => e.target.closest(".backdrop").remove() }, "OK")]);
    } catch (e) {
      problem.textContent = message(e);
      update();
    }
  });

  const shapes = scan.files.reduce((n, f) => n + f.shapes, 0);
  const close = modal("Match the QuPath classes", [
    h("div", {}, `${scan.files.length} file${scan.files.length === 1 ? "" : "s"} with ${shapes} annotation${shapes === 1 ? "" : "s"} for ${scan.files.length} image${scan.files.length === 1 ? "" : "s"}.`,
      scan.skipped ? h("span", { class: "faint" }, ` ${scan.skipped} other objects (detections, points, lines) are left out.`) : ""),
    scan.files.length > 1 && h("details", {}, h("summary", {}, "Files and images"),
      h("ul", { class: "files" }, scan.files.map((f) => h("li", {}, `${fileName(f.file)} → ${f.image}`, h("span", { class: "faint" }, ` (${f.shapes})`))))),
    scan.left_out.length > 0 && h("details", {}, h("summary", { class: "warn" }, `${scan.left_out.length} file${scan.left_out.length === 1 ? " is" : "s are"} left out`),
      h("ul", { class: "files" }, scan.left_out.map((f) => h("li", {}, fileName(f.file), h("span", { class: "faint" }, ` (${f.reason})`))))),
    scan.classes.length
      ? h("div", { class: "mapping" }, h("div", { class: "map-head" }, "QuPath class → class in this project"), rows)
      : h("p", { class: "warn" }, "The files contain no annotations that can be imported."),
    h("p", { class: "faint" }, "Classes set to “Don't import” are left out. A class that is missing here can be added in the class editor; then start the import again."),
    modeChoices,
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), go]);
  update();
}

openWorkspace();
    } catch (e) {
      problem.textContent = message(e);
      create.disabled = false;
    }
  });
  const close = modal("New annotation project", [
    h("label", { class: "field" }, h("span", {}, "Name"), name),
    folderField("Image folder", root, "All images below this folder (including subfolders) belong to the project. They are only read."),
    h("label", { class: "field" }, h("span", {}, "File types"), extensions, h("span", {}, "Separated by commas, e.g. .svs, .tif")),
    folderField("Save folder", save, "The project file and all annotations are written here. Choose an empty or new folder."),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), create]);
  name.focus();
}

function openProjectDialog() {
  const save = h("input", { type: "text", class: "mono", placeholder: "/path/to/the/save/folder", spellcheck: false });
  const problem = h("div", { class: "error" });
  const open = h("button", { class: "primary" }, "Open");
  open.addEventListener("click", async () => {
    try {
      await api("POST", "/projects/open", { json: { save: save.value.trim() } });
      close();
      // --------------------------------------------------------------------------- importing QuPath annotations

// Step 1 chooses the source: one GeoJSON file for the open image, or a folder whose files are matched to images by name.
// Step 2 shows what was found and maps every QuPath class to a project class (or leaves it out). The QuPath files are only read.
function importDialog(state) {
  let kind = state.image ? "file" : "folder";
  const path = h("input", { type: "text", class: "mono", placeholder: "/path/to/qupath/export", spellcheck: false });
  const problem = h("div", { class: "error" });
  const kinds = h("div", { class: "choices" });
  const drawKinds = () => kinds.replaceChildren(
    choice("kind", kind === "file", !state.image, () => ((kind = "file"), drawKinds()),
      "One file for the open image", state.image ? fileName(state.image) : "Open an image first"),
    choice("kind", kind === "folder", false, () => ((kind = "folder"), drawKinds()),
      "A folder of QuPath exports", "Each file is matched to the image with the same name, e.g. slide1.geojson to slide1.svs (subfolders count)."));
  drawKinds();
  const next = h("button", { class: "primary" }, "Next");
  next.addEventListener("click", async () => {
    next.disabled = true;
    const source = { path: path.value.trim(), image: kind === "file" ? state.image : undefined };
    try {
      const scan = await api("POST", "/import/scan", { json: source });
      close();
      mappingDialog(source, scan);
    } catch (e) {
      problem.textContent = message(e);
      next.disabled = false;
    }
  });
  const browse = h("button", { type: "button", onclick: async () => {
    const chosen = kind === "file"
      ? await pickFolder("Choose the QuPath GeoJSON file", path.value.trim() || undefined, { files: [".geojson", ".json"] })
      : await pickFolder("Choose the folder with the QuPath exports", path.value.trim() || undefined);
    if (chosen) path.value = chosen;
  } }, "Browse…");
  const close = modal("Import from QuPath", [
    h("p", { class: "faint" }, "Annotations exported from QuPath (File › Export objects as GeoJSON). The QuPath files are only read; the imported shapes become annotations of this project."),
    kinds,
    h("label", { class: "field" }, h("span", {}, "File or folder"), h("div", { class: "with-button" }, path, browse)),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), next]);
}

function choice(name, checked, disabled, onchange, title, detail) {
  return h("label", { class: "choice" + (disabled ? " disabled" : "") },
    h("input", { type: "radio", name, checked, disabled, onchange }),
    h("span", {}, h("strong", {}, title), detail && h("span", { class: "detail" }, detail)));
}

function mappingDialog(source, scan) {
  const classes = shared.get()?.project?.classes ?? [];
  const mapping = {};
  const existing = scan.files.filter((f) => f.existing > 0);
  let mode = existing.length ? null : "merge";
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary" }, "Import");
  const update = () => (go.disabled = !mode || !Object.values(mapping).some((v) => v !== null));

  const rows = scan.classes.map(({ name, count }) => {
    const same = classes.find((c) => c.name.toLowerCase() === name.toLowerCase());
    mapping[name] = same ? same.id : null;
    const select = h("select", { onchange: (e) => ((mapping[name] = e.target.value ? Number(e.target.value) : null), update()) },
      h("option", { value: "" }, "Don't import"),
      classes.map((c) => h("option", { value: c.id, selected: same?.id === c.id }, c.name)));
    return h("div", { class: "map-row" },
      h("div", { class: "map-name" }, name ? name : h("em", { class: "faint" }, "Unclassified"), h("span", { class: "faint" }, ` · ${count} shape${count === 1 ? "" : "s"}`)),
      h("div", { class: "map-target" }, h("span", { class: "faint" }, "→"), select));
  });

  const modeChoices = existing.length && h("div", { class: "choices" },
    h("div", {}, h("strong", {}, `${existing.length} of the images already ${existing.length === 1 ? "has" : "have"} annotations. What should happen to them?`)),
    choice("mode", false, false, () => ((mode = "merge"), update()), "Add the imported shapes", "The existing annotations stay."),
    choice("mode", false, false, () => ((mode = "replace"), update()), "Replace the existing annotations", "Images that get imported shapes lose their current annotations (undo brings them back)."));

  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const result = await api("POST", "/import/apply", { json: { ...source, mapping, mode } });
      close();
      modal("Import finished", h("p", {}, `${result.shapes} shape${result.shapes === 1 ? "" : "s"} imported into ${result.files} image${result.files === 1 ? "" : "s"}.`),
        [h("button", { class: "primary", onclick: (e) => e.target.closest(".backdrop").remove() }, "OK")]);
    } catch (e) {
      problem.textContent = message(e);
      update();
    }
  });

  const shapes = scan.files.reduce((n, f) => n + f.shapes, 0);
  const close = modal("Match the QuPath classes", [
    h("div", {}, `${scan.files.length} file${scan.files.length === 1 ? "" : "s"} with ${shapes} annotation${shapes === 1 ? "" : "s"} for ${scan.files.length} image${scan.files.length === 1 ? "" : "s"}.`,
      scan.skipped ? h("span", { class: "faint" }, ` ${scan.skipped} other objects (detections, points, lines) are left out.`) : ""),
    scan.files.length > 1 && h("details", {}, h("summary", {}, "Files and images"),
      h("ul", { class: "files" }, scan.files.map((f) => h("li", {}, `${fileName(f.file)} → ${f.image}`, h("span", { class: "faint" }, ` (${f.shapes})`))))),
    scan.left_out.length > 0 && h("details", {}, h("summary", { class: "warn" }, `${scan.left_out.length} file${scan.left_out.length === 1 ? " is" : "s are"} left out`),
      h("ul", { class: "files" }, scan.left_out.map((f) => h("li", {}, fileName(f.file), h("span", { class: "faint" }, ` (${f.reason})`))))),
    scan.classes.length
      ? h("div", { class: "mapping" }, h("div", { class: "map-head" }, "QuPath class → class in this project"), rows)
      : h("p", { class: "warn" }, "The files contain no annotations that can be imported."),
    h("p", { class: "faint" }, "Classes set to “Don't import” are left out. A class that is missing here can be added in the class editor; then start the import again."),
    modeChoices,
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), go]);
  update();
}

openWorkspace();
    } catch (e) {
      problem.textContent = message(e);
    }
  });
  const close = modal("Open a project", [folderField("Save folder", save, "The folder that contains the project's project.json."), problem],
    [h("button", { onclick: () => close() }, "Cancel"), open]);
}

// --------------------------------------------------------------------------- importing QuPath annotations

// Step 1 chooses the source: one GeoJSON file for the open image, or a folder whose files are matched to images by name.
// Step 2 shows what was found and maps every QuPath class to a project class (or leaves it out). The QuPath files are only read.
function importDialog(state) {
  let kind = state.image ? "file" : "folder";
  const path = h("input", { type: "text", class: "mono", placeholder: "/path/to/qupath/export", spellcheck: false });
  const problem = h("div", { class: "error" });
  const kinds = h("div", { class: "choices" });
  const drawKinds = () => kinds.replaceChildren(
    choice("kind", kind === "file", !state.image, () => ((kind = "file"), drawKinds()),
      "One file for the open image", state.image ? fileName(state.image) : "Open an image first"),
    choice("kind", kind === "folder", false, () => ((kind = "folder"), drawKinds()),
      "A folder of QuPath exports", "Each file is matched to the image with the same name, e.g. slide1.geojson to slide1.svs (subfolders count)."));
  drawKinds();
  const next = h("button", { class: "primary" }, "Next");
  next.addEventListener("click", async () => {
    next.disabled = true;
    const source = { path: path.value.trim(), image: kind === "file" ? state.image : undefined };
    try {
      const scan = await api("POST", "/import/scan", { json: source });
      close();
      mappingDialog(source, scan);
    } catch (e) {
      problem.textContent = message(e);
      next.disabled = false;
    }
  });
  const browse = h("button", { type: "button", onclick: async () => {
    const chosen = kind === "file"
      ? await pickFolder("Choose the QuPath GeoJSON file", path.value.trim() || undefined, { files: [".geojson", ".json"] })
      : await pickFolder("Choose the folder with the QuPath exports", path.value.trim() || undefined);
    if (chosen) path.value = chosen;
  } }, "Browse…");
  const close = modal("Import from QuPath", [
    h("p", { class: "faint" }, "Annotations exported from QuPath (File › Export objects as GeoJSON). The QuPath files are only read; the imported shapes become annotations of this project."),
    kinds,
    h("label", { class: "field" }, h("span", {}, "File or folder"), h("div", { class: "with-button" }, path, browse)),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), next]);
}

function choice(name, checked, disabled, onchange, title, detail) {
  return h("label", { class: "choice" + (disabled ? " disabled" : "") },
    h("input", { type: "radio", name, checked, disabled, onchange }),
    h("span", {}, h("strong", {}, title), detail && h("span", { class: "detail" }, detail)));
}

function mappingDialog(source, scan) {
  const classes = shared.get()?.project?.classes ?? [];
  const mapping = {};
  const existing = scan.files.filter((f) => f.existing > 0);
  let mode = existing.length ? null : "merge";
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary" }, "Import");
  const update = () => (go.disabled = !mode || !Object.values(mapping).some((v) => v !== null));

  const rows = scan.classes.map(({ name, count }) => {
    const same = classes.find((c) => c.name.toLowerCase() === name.toLowerCase());
    mapping[name] = same ? same.id : null;
    const select = h("select", { onchange: (e) => ((mapping[name] = e.target.value ? Number(e.target.value) : null), update()) },
      h("option", { value: "" }, "Don't import"),
      classes.map((c) => h("option", { value: c.id, selected: same?.id === c.id }, c.name)));
    return h("div", { class: "map-row" },
      h("div", { class: "map-name" }, name ? name : h("em", { class: "faint" }, "Unclassified"), h("span", { class: "faint" }, ` · ${count} shape${count === 1 ? "" : "s"}`)),
      h("div", { class: "map-target" }, h("span", { class: "faint" }, "→"), select));
  });

  const modeChoices = existing.length && h("div", { class: "choices" },
    h("div", {}, h("strong", {}, `${existing.length} of the images already ${existing.length === 1 ? "has" : "have"} annotations. What should happen to them?`)),
    choice("mode", false, false, () => ((mode = "merge"), update()), "Add the imported shapes", "The existing annotations stay."),
    choice("mode", false, false, () => ((mode = "replace"), update()), "Replace the existing annotations", "Images that get imported shapes lose their current annotations (undo brings them back)."));

  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const result = await api("POST", "/import/apply", { json: { ...source, mapping, mode } });
      close();
      modal("Import finished", h("p", {}, `${result.shapes} shape${result.shapes === 1 ? "" : "s"} imported into ${result.files} image${result.files === 1 ? "" : "s"}.`),
        [h("button", { class: "primary", onclick: (e) => e.target.closest(".backdrop").remove() }, "OK")]);
    } catch (e) {
      problem.textContent = message(e);
      update();
    }
  });

  const shapes = scan.files.reduce((n, f) => n + f.shapes, 0);
  const close = modal("Match the QuPath classes", [
    h("div", {}, `${scan.files.length} file${scan.files.length === 1 ? "" : "s"} with ${shapes} annotation${shapes === 1 ? "" : "s"} for ${scan.files.length} image${scan.files.length === 1 ? "" : "s"}.`,
      scan.skipped ? h("span", { class: "faint" }, ` ${scan.skipped} other objects (detections, points, lines) are left out.`) : ""),
    scan.files.length > 1 && h("details", {}, h("summary", {}, "Files and images"),
      h("ul", { class: "files" }, scan.files.map((f) => h("li", {}, `${fileName(f.file)} → ${f.image}`, h("span", { class: "faint" }, ` (${f.shapes})`))))),
    scan.left_out.length > 0 && h("details", {}, h("summary", { class: "warn" }, `${scan.left_out.length} file${scan.left_out.length === 1 ? " is" : "s are"} left out`),
      h("ul", { class: "files" }, scan.left_out.map((f) => h("li", {}, fileName(f.file), h("span", { class: "faint" }, ` (${f.reason})`))))),
    scan.classes.length
      ? h("div", { class: "mapping" }, h("div", { class: "map-head" }, "QuPath class → class in this project"), rows)
      : h("p", { class: "warn" }, "The files contain no annotations that can be imported."),
    h("p", { class: "faint" }, "Classes set to “Don't import” are left out. A class that is missing here can be added in the class editor; then start the import again."),
    modeChoices,
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), go]);
  update();
}

openWorkspace();
