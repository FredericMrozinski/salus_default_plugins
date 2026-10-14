import { Salus } from "../salus_sdk.js";

// The viewer shows pages that the backend renders as images (see backend/pdfs.py). The PDF file itself is never sent
// to the browser, and there is no download or print button. Zooming asks the server for a sharper rendering.
const salus = Salus.connect();
const file = salus.params?.file; // set by Salus when it opens this viewer for a file (see file_viewer_for in the manifest)

const $ = (id) => document.getElementById(id);
const PT_TO_PX = 96 / 72; // a PDF point at 100% zoom
const GAP = 12;           // margin below each page
const ZOOMS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4]; // the dropdown and the +/- buttons
const MIN_ZOOM = 0.1;
const MAX_ZOOM = 6;
const SETTLE_MS = 160; // after the last zoom change, pages are re-rendered sharp

const state = { doc: null, zoom: "fit", pages: [], visible: new Set(), settle: 0 };

function fail(text) {
  $("message").textContent = text;
  $("message").className = "message error";
  $("message").hidden = false;
}

if (!file) {
  fail("No file was given.");
} else {
  start().catch((error) => fail(`Could not open the PDF: ${error.body || error.message}`));
}

async function start() {
  const doc = await salus.http.json("GET", "/pdf/info", { query: { file } });
  state.doc = doc;
  $("file").textContent = doc.name;
  $("file").title = file;
  $("count").textContent = `/ ${doc.pages}`;
  $("page").max = String(doc.pages);
  $("message").hidden = true;
  const pagesEl = $("pages");
  pagesEl.hidden = false;

  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const index = Number(entry.target.dataset.index);
      if (entry.isIntersecting) {
        state.visible.add(index);
        load(index);
      } else {
        state.visible.delete(index);
        unload(index); // far pages give their image back; the backend keeps its rendering for when they return
      }
    }
  }, { root: pagesEl, rootMargin: "900px 0px" });

  state.pages = doc.sizes.map((size, index) => {
    const el = document.createElement("div");
    el.className = "page";
    el.dataset.index = String(index);
    const number = document.createElement("span");
    number.className = "number";
    number.textContent = String(index + 1);
    el.append(number);
    pagesEl.append(el);
    observer.observe(el);
    return { el, size, img: null, loadedScale: 0 };
  });

  fillZoomSelect();
  applyZoom("fit");
  pagesEl.addEventListener("scroll", () => requestAnimationFrame(updateCurrentPage), { passive: true });
  pagesEl.addEventListener("contextmenu", (e) => e.preventDefault());
  pagesEl.addEventListener("dragstart", (e) => e.preventDefault());
  pagesEl.addEventListener("wheel", onWheel, { passive: false });
  enablePanning(pagesEl);
  enableGestures(pagesEl);
  window.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    if (e.key === "+" || e.key === "=") (e.preventDefault(), step(1));
    else if (e.key === "-") (e.preventDefault(), step(-1));
    else if (e.key === "0") (e.preventDefault(), applyZoom("fit"));
    else if (e.key === "s" || e.key === "p") e.preventDefault();
  });
  window.addEventListener("resize", () => state.zoom === "fit" && applyZoom("fit"));
  $("zoom").addEventListener("change", (e) => applyZoom(e.target.value === "fit" ? "fit" : Number(e.target.value)));
  $("in").addEventListener("click", () => step(1));
  $("out").addEventListener("click", () => step(-1));
  $("page").addEventListener("change", (e) => goTo(Number(e.target.value) - 1));
}

function fillZoomSelect() {
  const select = $("zoom");
  select.replaceChildren(new Option("Fit width", "fit"), ...ZOOMS.map((z) => new Option(`${Math.round(z * 100)}%`, String(z))));
}

/** The zoom factor that makes the widest page fill the viewer. */
function fitZoom() {
  const widest = Math.max(...state.doc.sizes.map((s) => s[0]));
  const available = $("pages").clientWidth - 32;
  return Math.max(MIN_ZOOM, available / (widest * PT_TO_PX));
}

const currentZoom = () => (state.zoom === "fit" ? fitZoom() : state.zoom);
const clampZoom = (z) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));

/** The +/- buttons and keys: to the next preset above or below the current zoom, whatever the current zoom is. */
function step(direction) {
  const now = currentZoom();
  const next = direction > 0 ? ZOOMS.find((z) => z > now + 0.001) : [...ZOOMS].reverse().find((z) => z < now - 0.001);
  if (next) applyZoom(next);
}

/**
 * Sizes every page for the zoom. The point (x, y) of the viewer (default: top centre) stays where it is on screen, so
 * zooming with the pointer zooms around the pointer. Images are only stretched at first; they are re-rendered sharp
 * once the zoom has settled, which keeps pinching smooth and spares the server.
 */
function applyZoom(zoom, focus) {
  const pagesEl = $("pages");
  const x = focus?.x ?? pagesEl.clientWidth / 2;
  const y = focus?.y ?? 0;
  const anchor = anchorAt(pagesEl, x, y);
  state.zoom = zoom === "fit" ? "fit" : clampZoom(zoom);
  const factor = currentZoom() * PT_TO_PX;
  for (const page of state.pages) {
    page.el.style.width = `${Math.round(page.size[0] * factor)}px`;
    page.el.style.height = `${Math.round(page.size[1] * factor)}px`;
  }
  restoreAnchor(pagesEl, anchor, x, y);
  showZoom();
  updateCurrentPage();
  clearTimeout(state.settle);
  state.settle = setTimeout(() => state.visible.forEach(load), SETTLE_MS);
}

function showZoom() {
  const select = $("zoom");
  const preset = state.zoom === "fit" ? "fit" : ZOOMS.find((z) => Math.abs(z - state.zoom) < 0.001);
  select.querySelector("option[data-custom]")?.remove();
  if (preset === undefined) {
    const option = new Option(`${Math.round(state.zoom * 100)}%`, "custom");
    option.dataset.custom = "1";
    select.append(option);
    select.value = "custom";
  } else {
    select.value = String(preset);
  }
}

/** The place in the document under (x, y) of the viewer: a page and the fractions of it. */
function anchorAt(pagesEl, x, y) {
  const page = state.pages[pageAt(pagesEl.scrollTop + y)];
  return {
    page,
    fx: (pagesEl.scrollLeft + x - page.el.offsetLeft) / (page.el.offsetWidth || 1),
    fy: (pagesEl.scrollTop + y - page.el.offsetTop) / (page.el.offsetHeight || 1),
  };
}

function restoreAnchor(pagesEl, { page, fx, fy }, x, y) {
  pagesEl.scrollTop = page.el.offsetTop + fy * page.el.offsetHeight - y;
  pagesEl.scrollLeft = page.el.offsetLeft + fx * page.el.offsetWidth - x;
}

/**
 * Ctrl/Cmd + wheel is how browsers report a trackpad pinch (and Ctrl+scroll with a mouse). Pinch events are many and small,
 * so the zoom follows them continuously instead of jumping between presets. Big mouse-wheel notches are limited.
 */
function onWheel(e) {
  if (!e.ctrlKey && !e.metaKey) return; // plain scrolling stays plain
  e.preventDefault();
  const rect = $("pages").getBoundingClientRect();
  const delta = Math.max(-30, Math.min(30, e.deltaMode === 1 ? e.deltaY * 10 : e.deltaY));
  applyZoom(currentZoom() * Math.exp(-delta * 0.008), { x: e.clientX - rect.left, y: e.clientY - rect.top });
}

/** Safari reports a pinch as gesture events with a total scale instead of wheel events. */
function enableGestures(pagesEl) {
  let startZoom = 1;
  pagesEl.addEventListener("gesturestart", (e) => (e.preventDefault(), (startZoom = currentZoom())));
  pagesEl.addEventListener("gesturechange", (e) => {
    e.preventDefault();
    const rect = pagesEl.getBoundingClientRect();
    applyZoom(startZoom * e.scale, { x: e.clientX - rect.left, y: e.clientY - rect.top });
  });
  pagesEl.addEventListener("gestureend", (e) => e.preventDefault());
}

/** Click and drag to scroll, like the hand tool of a PDF viewer. */
function enablePanning(pagesEl) {
  let drag = null;
  pagesEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const rect = pagesEl.getBoundingClientRect();
    // A click on the scrollbar belongs to the scrollbar.
    if (e.clientX - rect.left >= pagesEl.clientWidth || e.clientY - rect.top >= pagesEl.clientHeight) return;
    drag = { x: e.clientX, y: e.clientY, left: pagesEl.scrollLeft, top: pagesEl.scrollTop };
    try {
      pagesEl.setPointerCapture(e.pointerId); // keeps the drag going outside the viewer
    } catch {
      // not every pointer can be captured; the drag still works inside the viewer
    }
    pagesEl.classList.add("panning");
    e.preventDefault();
  });
  pagesEl.addEventListener("pointermove", (e) => {
    if (!drag) return;
    pagesEl.scrollLeft = drag.left - (e.clientX - drag.x);
    pagesEl.scrollTop = drag.top - (e.clientY - drag.y);
  });
  const end = (e) => {
    if (!drag) return;
    drag = null;
    pagesEl.classList.remove("panning");
    try {
      pagesEl.releasePointerCapture(e.pointerId);
    } catch {
      // already released
    }
  };
  pagesEl.addEventListener("pointerup", end);
  pagesEl.addEventListener("pointercancel", end);
}

/** Index of the page that covers the given scroll position (binary search over the page tops). */
function pageAt(scrollTop) {
  const pages = state.pages;
  let low = 0;
  let high = pages.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (pages[mid].el.offsetTop - 12 <= scrollTop) low = mid;
    else high = mid - 1;
  }
  return low;
}

function updateCurrentPage() {
  const pagesEl = $("pages");
  const index = pageAt(pagesEl.scrollTop + pagesEl.clientHeight / 3);
  if (document.activeElement !== $("page")) $("page").value = String(index + 1);
}

function goTo(index) {
  if (!Number.isFinite(index)) return;
  const page = state.pages[Math.min(state.pages.length - 1, Math.max(0, index))];
  $("pages").scrollTop = page.el.offsetTop - GAP;
}

/** Renders at the screen's pixel density, so zoomed pages stay sharp. */
function requestScale() {
  return Math.min(8, Math.ceil(currentZoom() * PT_TO_PX * (window.devicePixelRatio || 1) * 4) / 4);
}

function load(index) {
  const page = state.pages[index];
  const scale = requestScale();
  if (page.img && page.loadedScale === scale) return;
  const img = new Image();
  img.draggable = false;
  img.alt = `Page ${index + 1}`;
  img.addEventListener("load", () => {
    // Swap only when the new rendering is there, so zooming never shows a blank page.
    page.img?.remove();
    page.img = img;
    page.el.prepend(img);
  });
  img.addEventListener("error", () => page.el.classList.add("failed"));
  page.loadedScale = scale;
  img.src = salus.http.url(`/pdf/page/${state.doc.id}/${index}`, { scale });
}

function unload(index) {
  const page = state.pages[index];
  page.img?.remove();
  page.img = null;
  page.loadedScale = 0;
}
