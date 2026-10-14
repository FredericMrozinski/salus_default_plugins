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
