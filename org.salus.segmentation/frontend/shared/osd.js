// Shared by the slide and image viewers: an OpenSeadragon viewer for a Deep Zoom image that the backend serves tile by tile.
// The page loads vendor/openseadragon.min.js first, which defines the global OpenSeadragon.

// The zoom, home and full-page buttons are ordinary <button>s, so they get the theme's raised button look in light and
// dark mode; OpenSeadragon's own buttons are images and can't follow the theme. The icons draw in currentColor.
const ICONS = {
  zoomIn: '<path d="M12 6v12M6 12h12"/>',
  zoomOut: '<path d="M6 12h12"/>',
  home: '<path d="M5 12l7-6 7 6M7 11v7h10v-7"/>',
  fullpage: '<path d="M6 10V6h4M14 6h4v4M18 14v4h-4M10 18H6v-4"/>',
};

const STYLE = `
/* OpenSeadragon sets display: inline-block on the control itself, so the row is an inner element */
.osd-controls-row { display: flex; gap: 6px; margin: 8px; }
.osd-controls button { display: grid; place-items: center; width: 30px; height: 30px; padding: 0; color: var(--salus-fg, #ddd); }
.osd-controls svg { width: 18px; height: 18px; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round;
  stroke-linejoin: round; fill: none; }
`;

function addStyle() {
  if (document.getElementById("osd-controls-style")) return;
  const style = document.createElement("style");
  style.id = "osd-controls-style";
  style.textContent = STYLE;
  document.head.append(style);
}

function addControls(viewer) {
  addStyle();
  const box = document.createElement("div");
  box.className = "osd-controls";
  const row = document.createElement("div");
  row.className = "osd-controls-row";
  box.append(row);
  const zoom = (factor) => { viewer.viewport.zoomBy(factor); viewer.viewport.applyConstraints(); };
  const items = [
    ["zoomIn", "Zoom in", () => zoom(viewer.zoomPerClick)],
    ["zoomOut", "Zoom out", () => zoom(1 / viewer.zoomPerClick)],
    ["home", "Fit to view", () => viewer.viewport.goHome()],
    ["fullpage", "Full screen", () => viewer.setFullScreen(!viewer.isFullPage())],
  ];
  for (const [key, label, run] of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[key]}</svg>`;
    button.addEventListener("click", run);
    row.append(button);
  }
  viewer.addControl(box, { anchor: OpenSeadragon.ControlAnchor.TOP_LEFT });
}

/**
 * @param {HTMLElement} element
 * @param {object} image  Deep Zoom description: {Url, Format, Overlap, TileSize, Size: {Width, Height}}
 * @param {object} [options]  more OpenSeadragon options, and `rotateKeys: false`, which turns off OpenSeadragon's R and
 *   Shift+R (rotate by 90°) and F (flip) keys; the other keys (WASD and the arrows pan, = and - zoom, 0 fits the image)
 *   keep working. The segmentation viewer turns them off, so the image cannot turn under the annotations.
 */
export function createViewer(element, image, { rotateKeys = true, ...options } = {}) {
  const viewer = OpenSeadragon({
    element,
    tileSources: { Image: { xmlns: "http://schemas.microsoft.com/deepzoom/2008", ...image } },
    showNavigationControl: false, // replaced by addControls
    showNavigator: true,
    navigatorPosition: "TOP_RIGHT",
    navigatorSizeRatio: 0.18,
    animationTime: 0.35,
    blendTime: 0.1,
    maxZoomPixelRatio: 2,
    minZoomImageRatio: 0.8,
    visibilityRatio: 0.5,
    imageLoaderLimit: 8,
    gestureSettingsMouse: { clickToZoom: false },
    ...options,
  });
  if (options.showNavigationControl !== true) addControls(viewer);
  if (!rotateKeys) {
    viewer.addHandler("canvas-key", (event) => {
      const code = event.originalEvent?.code;
      if (code === "KeyR" || code === "KeyF") event.preventDefaultAction = true;
    });
  }
  return viewer;
}

/** Image pixels per screen pixel, as a percentage: 100% shows the image at its real size. */
export function zoomPercent(viewer) {
  const viewport = viewer.viewport;
  return viewport.viewportToImageZoom(viewport.getZoom(true)) * 100;
}
