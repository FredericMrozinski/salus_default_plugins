// The annotation pane (center): the image in OpenSeadragon, and a canvas on top that draws the annotations and the
// shape being drawn. Tools and classes are shared state (chosen in the tools and class panels); the drawn shapes go to the
// backend, which keeps them as GeoJSON in image pixel coordinates.
import { api, fileName, followState, message as errorText, onEvent, salus, setState } from "../shared/common.js";
import { createViewer, zoomPercent } from "../shared/osd.js";

const container = document.getElementById("viewer");
const messageEl = document.getElementById("message");
const statusEl = document.getElementById("status");

let state = null;
let image = null; // the image shown (path relative to the project root)
let viewer = null;
let overlay = null; // the canvas on top of the image
let offset = [0, 0]; // level-0 position of the image's top-left tile (OpenSlide bounds); annotations use level-0 coordinates
let shapes = []; // [{id, class_id, polygons: [[ring, ...], ...], bbox: [x0, y0, x1, y1]}]
let draft = null; // polygon being drawn: {points: [[x, y], ...]} in image pixels
let stroke = null; // eraser stroke being drawn: {points, radius} in image pixels
// Smart brush stroke: the backend segments each dab; `dabs` are its masks for the preview, placed in image pixels.
// {id, mode: "paint" | "erase", dabs: [{x, y, w, h, bitmap}], pending: [x, y] | null, busy, ended, polygon?}
// A smart polygon is a stroke too, with the drawn `polygon` (image pixels), whose dabs the backend places itself.
let smart = null;
let preview = null; // offscreen canvas the smart brush preview is tinted in
let press = null; // the mouse button is down: {x, y, moved, startedEmpty}
let pointer = null; // last mouse position on the canvas (screen pixels), or null outside
let space = false; // Space is held: pan whatever the tool
let hint = "";
let openToken = 0;
let loadToken = 0;

const CLOSE_DISTANCE = 10; // screen pixels: a click this close to the first point closes the polygon
const DRAG_THRESHOLD = 4; // screen pixels the mouse must move before a press counts as freehand drawing
const SAMPLE_DISTANCE = 3; // screen pixels between freehand points
const SMART_INNER = 16; // screen pixels; the smart brush's inner radius, as in backend/smartbrush.py (INNER)
const isPolygonTool = (tool) => tool === "polygon" || tool === "smartpolygon"; // both draw a polygon
const isSmartBrush = (tool) => tool === "smart" || tool === "smarteraser"; // both paint dabs while dragging

followState((next) => {
  const previous = state;
  state = next;
  if (!next.project) return closeImage("Open a project and choose an image in the image browser.");
  if (next.image !== image) return void openImage(next.image);
  if (previous && previous.tool !== next.tool) cancel(false); // a finished smart stroke still being saved is kept
  if (previous && previous.class_id !== next.class_id && draft) hint = "";
  updateCursor();
  redraw();
});

onEvent((event) => {
  if (event.type === "annotations" && event.image === image) loadAnnotations();
});

// --------------------------------------------------------------------------- opening an image

function closeImage(text) {
  openToken++;
  image = null;
  shapes = [];
  draft = stroke = null;
  cancelSmart();
  viewer?.destroy();
  viewer = overlay = null;
  messageEl.textContent = text;
  messageEl.className = "message";
  renderStatus();
}

async function openImage(path) {
  closeImage(path ? "Loading…" : "Choose an image in the image browser.");
  if (!path) return;
  const token = openToken;
  image = path;
  let slide;
  try {
    slide = await api("GET", "/slide/info", { query: { image: path } });
  } catch (error) {
    if (token !== openToken) return;
    messageEl.textContent = `Could not open ${fileName(path)}: ${errorText(error)}`;
    messageEl.className = "message error";
    return;
  }
  if (token !== openToken) return;
  messageEl.textContent = "";
  offset = slide.offset ?? [0, 0];
  viewer = createViewer(container, {
    Url: salus.http.url(`/slide/tiles/${slide.id}/`),
    Format: slide.format,
    Overlap: slide.overlap,
    TileSize: slide.tile_size,
    Size: { Width: slide.width, Height: slide.height },
  });
  overlay = document.createElement("canvas");
  overlay.className = "overlay";
  viewer.canvas.appendChild(overlay); // above the image, below OpenSeadragon's buttons and navigator
  for (const name of ["animation", "animation-finish", "update-viewport", "resize", "open"]) viewer.addHandler(name, redraw);
  viewer.addHandler("animation", renderStatus);
  viewer.addHandler("open", renderStatus);
  // While drawing, the left button draws instead of panning; the mouse wheel still zooms.
  const block = (event) => { if (drawing()) event.preventDefaultAction = true; };
  for (const name of ["canvas-drag", "canvas-drag-end", "canvas-click", "canvas-double-click"]) viewer.addHandler(name, block);
  updateCursor();
  await loadAnnotations();
}

async function loadAnnotations() {
  if (!image) return;
  const token = ++loadToken;
  const path = image;
  try {
    const data = await api("GET", "/annotations", { query: { image: path } });
    if (token !== loadToken || path !== image) return;
    shapes = data.features.map(toShape).filter(Boolean);
  } catch (error) {
    hint = `Could not load the annotations: ${errorText(error)}`;
  }
  redraw();
  renderStatus();
}

function toShape(feature) {
  const g = feature.geometry;
  const polygons = g?.type === "Polygon" ? [g.coordinates] : g?.type === "MultiPolygon" ? g.coordinates : null;
  if (!polygons) return null;
  const bbox = [Infinity, Infinity, -Infinity, -Infinity];
  for (const polygon of polygons) for (const [x, y] of polygon[0] ?? []) {
    if (x < bbox[0]) bbox[0] = x;
    if (y < bbox[1]) bbox[1] = y;
    if (x > bbox[2]) bbox[2] = x;
    if (y > bbox[3]) bbox[3] = y;
  }
  return { id: feature.id, class_id: feature.properties?.class_id, polygons, bbox };
}

// --------------------------------------------------------------------------- coordinates

/** Screen pixels (relative to the viewer) = origin + level-0 pixels * scale. The viewer never rotates.
 *  OpenSeadragon's image starts at `offset` of level 0 when the slide has bounds (MRXS, for example). */
function transform() {
  const vp = viewer.viewport;
  const o = vp.imageToViewerElementCoordinates(new OpenSeadragon.Point(0, 0));
  const p = vp.imageToViewerElementCoordinates(new OpenSeadragon.Point(1000, 0));
  const s = (p.x - o.x) / 1000;
  return { ox: o.x - offset[0] * s, oy: o.y - offset[1] * s, s };
}

const toImage = (x, y, t = transform()) => [(x - t.ox) / t.s, (y - t.oy) / t.s];
const toScreen = ([x, y], t = transform()) => [t.ox + x * t.s, t.oy + y * t.s];

function local(event) {
  const rect = container.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

// --------------------------------------------------------------------------- drawing

let frame = 0;
function redraw() {
  if (!frame) frame = requestAnimationFrame(() => ((frame = 0), draw()));
}

function draw() {
  if (!overlay || !viewer) return;
  const dpr = window.devicePixelRatio || 1;
  const width = container.clientWidth, height = container.clientHeight;
  if (overlay.width !== Math.round(width * dpr) || overlay.height !== Math.round(height * dpr)) {
    overlay.width = Math.round(width * dpr);
    overlay.height = Math.round(height * dpr);
  }
  const ctx = overlay.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  if (viewer.world.getItemCount() === 0) return;
  const t = transform();
  const colors = new Map((state?.project?.classes ?? []).map((c) => [c.id, c.color]));
  const opacity = state?.opacity ?? 0.35;
  const [vx0, vy0] = toImage(0, 0, t), [vx1, vy1] = toImage(width, height, t);

  // The shapes, in image coordinates.
  ctx.setTransform(dpr * t.s, 0, 0, dpr * t.s, dpr * t.ox, dpr * t.oy);
  ctx.lineJoin = "round";
  for (const shape of shapes) {
    const [x0, y0, x1, y1] = shape.bbox;
    if (x1 < vx0 || x0 > vx1 || y1 < vy0 || y0 > vy1) continue;
    const color = colors.get(shape.class_id) ?? "#888888";
    ctx.beginPath();
    for (const polygon of shape.polygons) for (const ring of polygon) {
      ring.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.closePath();
    }
    if (opacity > 0) {
      ctx.globalAlpha = opacity;
      ctx.fillStyle = color;
      ctx.fill("evenodd");
      ctx.globalAlpha = 1;
    }
    ctx.lineWidth = 1.5 / t.s;
    ctx.strokeStyle = color;
    ctx.stroke();
  }

  // The eraser stroke: what will be cut away.
  if (stroke && stroke.points.length) {
    ctx.beginPath();
    stroke.points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (stroke.points.length === 1) ctx.lineTo(stroke.points[0][0] + 0.01, stroke.points[0][1]);
    ctx.lineCap = "round";
    ctx.lineWidth = stroke.radius * 2;
    ctx.strokeStyle = "rgba(255, 255, 255, 0.35)";
    ctx.stroke();
  }

  // The smart brush stroke: its dab masks drawn opaque in an offscreen canvas, tinted, then blended in once,
  // so overlapping dabs do not add up.
  const classColor = colors.get(state?.class_id) ?? "#ffffff";
  if (smart && (smart.dabs.length || smart.polygon)) {
    preview ??= document.createElement("canvas");
    preview.width = overlay.width;
    preview.height = overlay.height;
    const p = preview.getContext("2d");
    p.setTransform(dpr * t.s, 0, 0, dpr * t.s, dpr * t.ox, dpr * t.oy);
    p.imageSmoothingEnabled = false;
    for (const d of smart.dabs) {
      if (d.x + d.w < vx0 || d.x > vx1 || d.y + d.h < vy0 || d.y > vy1) continue;
      p.drawImage(d.bitmap, d.x, d.y, d.w, d.h);
    }
    if (smart.polygon) {
      p.beginPath();
      smart.polygon.forEach(([x, y], i) => (i ? p.lineTo(x, y) : p.moveTo(x, y)));
      p.fillStyle = "#ffffff";
      p.fill();
    }
    p.setTransform(1, 0, 0, 1, 0, 0);
    p.globalCompositeOperation = "source-in";
    p.fillStyle = smart.mode === "erase" ? "#ffffff" : classColor;
    p.fillRect(0, 0, preview.width, preview.height);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = Math.max(opacity, 0.35);
    ctx.drawImage(preview, 0, 0);
    ctx.globalAlpha = 1;
  }

  // Screen coordinates from here on.
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (draft && draft.points.length) {
    const points = draft.points.map((p) => toScreen(p, t));
    ctx.beginPath();
    points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (pointer && !press) ctx.lineTo(pointer.x, pointer.y);
    ctx.lineWidth = 2;
    ctx.strokeStyle = classColor;
    ctx.setLineDash([6, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
    if (points.length < 400) {
      ctx.fillStyle = classColor;
      for (const [x, y] of points) ctx.fillRect(x - 2.5, y - 2.5, 5, 5);
    }
    if (nearFirst()) {
      ctx.beginPath();
      ctx.arc(points[0][0], points[0][1], 7, 0, Math.PI * 2);
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#ffffff";
      ctx.stroke();
    }
  }
  if (state?.tool === "eraser" && pointer && !space) {
    ctx.beginPath();
    ctx.arc(pointer.x, pointer.y, state.brush / 2, 0, Math.PI * 2);
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "rgba(0, 0, 0, 0.7)";
    ctx.stroke();
    ctx.lineWidth = 1;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();
  }
  if (isSmartBrush(state?.tool) && pointer && !space) {
    // The inner circle, where the seeds are. Blue while erasing.
    const erasing = smart && !smart.polygon ? smart.mode === "erase" : state.tool === "smarteraser" || shift;
    ctx.beginPath();
    ctx.arc(pointer.x, pointer.y, SMART_INNER, 0, Math.PI * 2);
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = "rgba(0, 0, 0, 0.6)";
    ctx.stroke();
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = erasing ? "#5aa9ff" : "#ffffff";
    ctx.stroke();
  }
}

function nearFirst() {
  if (!draft || draft.points.length < 3 || !pointer) return false;
  const [x, y] = toScreen(draft.points[0]);
  return Math.hypot(x - pointer.x, y - pointer.y) <= CLOSE_DISTANCE;
}

function renderStatus() {
  const parts = [];
  if (image) parts.push(fileName(image));
  if (viewer && viewer.world.getItemCount()) parts.push(`zoom ${Math.round(zoomPercent(viewer))}%`);
  if (image) parts.push(`${shapes.length} shape${shapes.length === 1 ? "" : "s"}`);
  statusEl.replaceChildren(parts.join("  |  "), hint ? Object.assign(document.createElement("span"), { className: "hint", textContent: `  |  ${hint}` }) : "");
}

function updateCursor() {
  if (!viewer) return;
  const tool = state?.tool;
  viewer.canvas.style.cursor = space || tool === "pan" ? "grab" : tool === "eraser" || isSmartBrush(tool) ? "none" : "crosshair";
}

// --------------------------------------------------------------------------- input

let shift = false; // Shift is held: the smart brush erases
const drawing = () => !!(viewer && image && !space && ["polygon", "eraser", "smart", "smarteraser", "smartpolygon"].includes(state?.tool));
const inCanvas = (event) => viewer && viewer.canvas.contains(event.target);

function setHint(text) {
  hint = text;
  renderStatus();
}

container.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || !drawing() || !inCanvas(event)) return;
  const p = local(event);
  pointer = p;
  if (isPolygonTool(state.tool)) {
    if (state.class_id == null) return setHint("Choose a class in the class editor first.");
    press = { ...p, moved: false, startedEmpty: !draft };
  } else if (isSmartBrush(state.tool)) {
    const mode = state.tool === "smarteraser" || event.shiftKey ? "erase" : "paint";
    if (mode === "paint" && state.class_id == null) return setHint("Choose a class in the class editor first.");
    if (smart) return setHint(smart.polygon ? "Wait until the smart polygon is done." : ""); // the previous stroke is still being saved
    press = { ...p, moved: true };
    smart = { id: crypto.randomUUID(), mode, dabs: [], pending: toImage(p.x, p.y), busy: false, ended: false, scale: transform().s };
    pumpSmart();
  } else {
    press = { ...p, moved: true };
    const t = transform();
    stroke = { points: [toImage(p.x, p.y, t)], radius: state.brush / 2 / t.s };
  }
  setHint("");
  redraw();
}, true);

container.addEventListener("pointermove", (event) => {
  const p = local(event);
  pointer = inCanvas(event) || press ? p : null;
  if (press && drawing()) {
    const t = transform();
    if (isPolygonTool(state.tool)) {
      if (!press.moved && Math.hypot(p.x - press.x, p.y - press.y) > DRAG_THRESHOLD) {
        press.moved = true;
        draft ??= { points: [] };
        if (press.startedEmpty) draft.points.push(toImage(press.x, press.y, t));
      }
      if (press.moved) addPoint(draft.points, p, t);
    } else if (isSmartBrush(state.tool)) {
      if (smart && !smart.ended) {
        smart.pending = toImage(p.x, p.y, t);
        pumpSmart();
      }
    } else if (stroke) {
      addPoint(stroke.points, p, t);
    }
  }
  redraw();
}, true);

function addPoint(points, p, t) {
  const last = points[points.length - 1];
  if (last) {
    const [lx, ly] = toScreen(last, t);
    if (Math.hypot(lx - p.x, ly - p.y) < SAMPLE_DISTANCE) return;
  }
  points.push(toImage(p.x, p.y, t));
}

window.addEventListener("pointerup", (event) => {
  if (!press) return;
  const p = local(event);
  const wasPress = press;
  press = null;
  if (!viewer) return;
  if (isPolygonTool(state.tool)) {
    if (wasPress.moved) {
      if (wasPress.startedEmpty) finishPolygon(); // a freehand shape: releasing closes it
    } else if (nearFirst()) {
      finishPolygon();
    } else {
      draft ??= { points: [] };
      draft.points.push(toImage(p.x, p.y));
    }
  } else if (isSmartBrush(state.tool)) {
    if (smart && !smart.ended) {
      smart.ended = true;
      pumpSmart();
    }
  } else if (stroke) {
    erase();
  }
  redraw();
}, true);

container.addEventListener("dblclick", (event) => {
  if (isPolygonTool(state?.tool) && draft && inCanvas(event)) finishPolygon();
}, true);

container.addEventListener("pointerleave", () => {
  if (!press) pointer = null;
  redraw();
});

window.addEventListener("keydown", (event) => {
  if (event.target.closest?.("input, textarea, select")) return;
  const mod = event.metaKey || event.ctrlKey;
  if (event.key === "Shift" && !shift) {
    shift = true;
    redraw();
  }
  if (event.key === " " && !space) {
    space = true;
    updateCursor();
    redraw();
    event.preventDefault();
  } else if (event.key === "Escape") {
    cancel();
  } else if ((event.key === "Backspace" || event.key === "Delete") && draft) {
    draft.points.pop();
    if (!draft.points.length) draft = null;
    redraw();
    event.preventDefault();
  } else if (event.key === "Enter" && draft) {
    finishPolygon();
  } else if (mod && event.key.toLowerCase() === "z") {
    history(event.shiftKey ? "redo" : "undo");
    event.preventDefault();
  } else if (mod && event.key.toLowerCase() === "y") {
    history("redo");
    event.preventDefault();
  } else if (!mod && !event.altKey) {
    const tool = { v: "pan", p: "polygon", e: "eraser", b: "smart", x: "smarteraser", s: "smartpolygon" }[event.key.toLowerCase()];
    if (tool && state?.project) setState({ tool }).catch(() => undefined);
  }
});

window.addEventListener("keyup", (event) => {
  if (event.key === "Shift") {
    shift = false;
    redraw();
  }
  if (event.key === " ") {
    space = false;
    updateCursor();
    redraw();
  }
});

/** Drops what is being drawn. `all`: also a smart stroke that has ended and is being finished (Esc). */
function cancel(all = true) {
  draft = null;
  stroke = null;
  press = null;
  if (all || !smart?.ended) cancelSmart();
  redraw();
}

// --------------------------------------------------------------------------- talking to the backend

async function finishPolygon() {
  if (!draft) return;
  const t = transform();
  // Drop repeated points (a double click adds the same point twice).
  const points = [];
  for (const p of draft.points) {
    const last = points[points.length - 1];
    if (!last || Math.hypot((p[0] - last[0]) * t.s, (p[1] - last[1]) * t.s) >= 1) points.push(p);
  }
  if (state.tool === "smartpolygon" && smart && points.length >= 3) return setHint("Wait until the smart polygon is done.");
  draft = null;
  redraw();
  if (points.length < 3) return setHint("A polygon needs at least three points.");
  if (state.tool === "smartpolygon") return smartPolygon(points, t.s);
  try {
    // Freehand shapes have many points; drop those that change the outline by less than half a screen pixel.
    await api("POST", "/annotations/polygon", { json: { image, class_id: state.class_id, points, simplify: 0.5 / t.s } });
    await loadAnnotations();
  } catch (error) {
    setHint(errorText(error));
  }
}

async function erase() {
  const current = stroke;
  try {
    const result = await api("POST", "/annotations/erase", { json: { image, points: current.points, radius: current.radius } });
    if (result.changed) await loadAnnotations();
  } catch (error) {
    setHint(errorText(error));
  } finally {
    if (stroke === current) stroke = null;
    redraw();
  }
}

// The smart brush sends one point at a time: while the backend works on a dab, the pointer's latest position waits in
// `pending` (positions in between are skipped; the backend places the dabs along the way itself).
async function pumpSmart() {
  const current = smart;
  if (!current || current.busy) return;
  if (current.pending) {
    const [x, y] = current.pending;
    current.pending = null;
    current.busy = true;
    try {
      const result = await api("POST", "/smartbrush/dab", { json: { image, stroke: current.id, x, y, scale: current.scale, mode: current.mode } });
      const dabs = await Promise.all(result.dabs.map(async (d) => ({ ...d, bitmap: await bitmapOf(d.png) })));
      if (smart !== current) return;
      current.dabs.push(...dabs);
      redraw();
    } catch (error) {
      if (smart === current) {
        setHint(errorText(error));
        cancelSmart();
      }
      return;
    } finally {
      current.busy = false;
    }
    return pumpSmart();
  }
  if (current.ended) await commitSmart(current);
}

// A smart polygon: the backend places the dabs along the outline and returns them in batches of about a second;
// the viewer asks until none remain, showing each batch, then commits the polygon with its dabs.
async function smartPolygon(points, scale) {
  const current = smart = { id: crypto.randomUUID(), mode: "paint", dabs: [], pending: null, busy: true, ended: true, scale, polygon: points };
  setHint("Smart polygon: following the outline…");
  redraw();
  try {
    let json = { image, stroke: current.id, points, scale };
    for (;;) {
      const result = await api("POST", "/smartbrush/outline", { json });
      const dabs = await Promise.all(result.dabs.map(async (d) => ({ ...d, bitmap: await bitmapOf(d.png) })));
      if (smart !== current) return; // cancelled
      current.dabs.push(...dabs);
      redraw();
      if (!result.remaining) break;
      json = { image, stroke: current.id };
    }
  } catch (error) {
    if (smart === current) {
      setHint(errorText(error));
      cancelSmart();
    }
    return;
  }
  setHint("");
  await commitSmart(current);
}

async function bitmapOf(base64) {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  return createImageBitmap(new Blob([bytes], { type: "image/png" }));
}

async function commitSmart(current) {
  current.busy = true; // nothing more is sent for this stroke
  try {
    const result = await api("POST", "/smartbrush/commit", { json: { image, stroke: current.id, class_id: state.class_id } });
    if (result.changed) await loadAnnotations();
  } catch (error) {
    setHint(errorText(error));
  } finally {
    if (smart === current) smart = null; // the preview goes once the shapes are loaded
    redraw();
  }
}

function cancelSmart() {
  if (!smart) return;
  const id = smart.id;
  smart = null;
  api("POST", "/smartbrush/cancel", { json: { stroke: id } }).catch(() => undefined);
}

async function history(which) {
  if (!image) return;
  try {
    await api("POST", `/annotations/${which}`, { json: { image } });
    await loadAnnotations();
  } catch (error) {
    setHint(errorText(error));
  }
}

window.addEventListener("resize", redraw);
