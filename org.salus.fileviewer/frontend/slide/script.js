import { Salus } from "../salus_sdk.js";
import { createViewer, zoomPercent } from "../osd.js";

const salus = Salus.connect();
const statusEl = document.getElementById("status");

// Salus opened this viewer for a file and passes its path in the parameters (see `file_viewer_for` in the manifest).
const file = salus.params?.file;

let slide = null; // metadata from GET /slide/info
let viewer = null;

function render() {
    const parts = [slide?.name ?? "Loading slide..."];
    if (viewer && viewer.world.getItemCount() > 0) {
        const percent = zoomPercent(viewer);
        // At 100% the scan is shown at its native (objective) magnification.
        const magnification = slide?.objective ? ` (${((percent / 100) * Number(slide.objective)).toFixed(1)}x)` : "";
        parts.push(`zoom ${Math.round(percent)}%${magnification}`);
    }
    statusEl.textContent = parts.join("  |  ");
}

if (!file) {
    statusEl.textContent = "No slide was given. Open a slide file from the Dataset Workbench.";
    throw new Error("no file parameter");
}

// A slide without a (complete) pyramid has to be read at full resolution once before it can be shown zoomed out, which
// takes minutes for a large one: the backend says so from the file's metadata (GET /slide/check), and the user decides.
const check = await salus.http.json("GET", "/slide/check", { query: { file } }).catch(() => null);
if (check?.warn) {
    statusEl.textContent = "Waiting for your answer…";
    if (!(await askToOpen(check))) {
        statusEl.textContent = "Not opened: this slide has no pyramidal image structure. Add one with the Pharaoh plugin, then open it again.";
        throw new Error("not opened");
    }
    statusEl.textContent = "Opening the slide; this can take a few minutes…";
}

try {
    slide = await salus.http.json("GET", "/slide/info", { query: { file } });
} catch (error) {
    statusEl.textContent = `Could not load the slide: ${error.body || error.message}`;
    throw error;
}

// A Deep Zoom image described directly; OpenSeadragon then requests the tiles as plain GETs under
// <Url><level>/<col>_<row>.jpeg. The Salus framework forwards those to the route the backend opened.
// The slide id in the URL keeps cached tiles from ever being mixed up between slides.
viewer = createViewer(document.getElementById("viewer"), {
    Url: salus.http.url(`/slide/tiles/${slide.id}/`),
    Format: slide.format,
    Overlap: slide.overlap,
    TileSize: slide.tile_size,
    Size: { Width: slide.width, Height: slide.height },
});

viewer.addHandler("open", render);
// "animation" fires every frame of an animated zoom or pan, "animation-finish" at the end.
viewer.addHandler("animation", render);
viewer.addHandler("animation-finish", render);
viewer.addHandler("open-failed", (event) => {
    slide = { name: `Could not open the slide: ${event.message}` };
    // Neither the viewer nor its tiles offer a way to save the slide.
document.getElementById("viewer").addEventListener("contextmenu", (e) => e.preventDefault());
render();
});

// Neither the viewer nor its tiles offer a way to save the slide.
document.getElementById("viewer").addEventListener("contextmenu", (e) => e.preventDefault());
render();


/** Asks whether to open a slide without a pyramid anyway. Resolves true (open) or false. */
function askToOpen(check) {
    const size = `${check.width.toLocaleString()} × ${check.height.toLocaleString()} px`;
    const levels = check.levels > 1 ? `its smallest level is still ${check.smallest[0].toLocaleString()} px wide` : "it has a single level";
    return new Promise((resolve) => {
        const open = el("button", { className: "primary", textContent: "Open anyway" });
        const cancel = el("button", { textContent: "Cancel" });
        const backdrop = el("div", { className: "backdrop" },
            el("div", { className: "dialog", role: "dialog", "aria-modal": "true" },
                el("header", { textContent: "No pyramidal image structure" }),
                el("div", { className: "content" },
                    el("p", { textContent: `This slide (${size}) has no pyramidal image structure: ${levels}. Opening it can take a few minutes.` }),
                    el("p", { textContent: "Instead, a pyramidal structure can be added with the Pharaoh plugin; the slide then opens at once." }),
                    el("p", { textContent: "Would you still like to proceed and open the file?" })),
                el("footer", {}, cancel, open)));
        const done = (answer) => {
            backdrop.remove();
            resolve(answer);
        };
        open.addEventListener("click", () => done(true));
        cancel.addEventListener("click", () => done(false));
        backdrop.addEventListener("keydown", (event) => event.key === "Escape" && done(false));
        document.body.append(backdrop);
        cancel.focus();
    });
}

function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
        if (key.includes("-")) node.setAttribute(key, value);
        else node[key] = value;
    }
    node.append(...children);
    return node;
}
