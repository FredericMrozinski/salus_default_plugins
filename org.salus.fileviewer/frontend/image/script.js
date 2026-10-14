import { Salus } from "../salus_sdk.js";
import { createViewer, zoomPercent } from "../osd.js";

const salus = Salus.connect();
const statusEl = document.getElementById("status");
const file = salus.params?.file; // set by Salus when it opens this viewer for a file

let picture = null; // metadata from GET /image/info
let viewer = null;

function render() {
    const parts = [picture?.name ?? "Loading image..."];
    if (picture?.width) parts.push(`${picture.width} × ${picture.height} px`);
    if (viewer && viewer.world.getItemCount() > 0) parts.push(`zoom ${Math.round(zoomPercent(viewer))}%`);
    statusEl.textContent = parts.join("  |  ");
}

if (!file) {
    statusEl.textContent = "No image was given.";
    throw new Error("no file parameter");
}

try {
    picture = await salus.http.json("GET", "/image/info", { query: { file } });
} catch (error) {
    statusEl.textContent = `Could not load the image: ${error.body || error.message}`;
    throw error;
}

// The backend decodes the file and serves it as Deep Zoom tiles (see backend/pictures.py): the original file is never
// sent to the browser, and big images load quickly because only the visible tiles are fetched.
viewer = createViewer(document.getElementById("viewer"), {
    Url: salus.http.url(`/image/tiles/${picture.id}/`),
    Format: picture.format,
    Overlap: picture.overlap,
    TileSize: picture.tile_size,
    Size: { Width: picture.width, Height: picture.height },
}, {
    maxZoomPixelRatio: 8, // look at single pixels
});

viewer.addHandler("open", render);
viewer.addHandler("animation", render);
viewer.addHandler("animation-finish", render);
viewer.addHandler("open-failed", (event) => {
    picture = { name: `Could not open the image: ${event.message}` };
    render();
});
// Neither the viewer nor its tiles offer a way to save the image.
document.getElementById("viewer").addEventListener("contextmenu", (e) => e.preventDefault());
render();
