// Shared by the slide and image viewers: an OpenSeadragon viewer for a Deep Zoom image that the backend serves tile by tile.
// The page loads vendor/openseadragon.min.js first, which defines the global OpenSeadragon.

// OpenSeadragon's own zoom, home and full-page buttons. Their default images are files, so these are inline SVGs and the
// plugin needs no extra files. prefixUrl is empty because the images are complete data: URLs.
function icon(paths, opacity) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 28 28">` +
    `<rect x="1" y="1" width="26" height="26" rx="5" fill="#1b1e25" fill-opacity="0.75" stroke="#fff" stroke-opacity="${opacity}"/>` +
    `<g stroke="#fff" stroke-opacity="${opacity + 0.2}" stroke-width="2" stroke-linecap="round" fill="none">${paths}</g></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
const buttons = (paths) => ({ REST: icon(paths, 0.5), GROUP: icon(paths, 0.5), HOVER: icon(paths, 0.9), DOWN: icon(paths, 1) });

/**
 * @param {HTMLElement} element
 * @param {object} image  Deep Zoom description: {Url, Format, Overlap, TileSize, Size: {Width, Height}}
 * @param {object} [options]  more OpenSeadragon options
 */
export function createViewer(element, image, options = {}) {
  return OpenSeadragon({
    element,
    tileSources: { Image: { xmlns: "http://schemas.microsoft.com/deepzoom/2008", ...image } },
    showNavigationControl: true,
    prefixUrl: "",
    navImages: {
      zoomIn: buttons('<path d="M14 8v12M8 14h12"/>'),
      zoomOut: buttons('<path d="M8 14h12"/>'),
      home: buttons('<path d="M7 14l7-6 7 6M9 13v7h10v-7"/>'),
      fullpage: buttons('<path d="M8 12V8h4M16 8h4v4M20 16v4h-4M12 20H8v-4"/>'),
    },
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
}

/** Image pixels per screen pixel, as a percentage: 100% shows the image at its real size. */
export function zoomPercent(viewer) {
  const viewport = viewer.viewport;
  return viewport.viewportToImageZoom(viewport.getZoom(true)) * 100;
}
