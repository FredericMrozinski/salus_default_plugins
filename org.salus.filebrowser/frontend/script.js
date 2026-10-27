import { Salus } from "./salus_sdk.js";

const salus = Salus.connect();
const pathInput = document.getElementById("path");
const upButton = document.getElementById("up");
const list = document.getElementById("entries");
const message = document.getElementById("message");

let current = null; // the listing on screen

const joinPath = (directory, name) => (directory.endsWith("/") ? directory : directory + "/") + name;

function formatSize(bytes) {
  if (bytes == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

// Material icons (24px grid, drawn in the text color so they follow the theme).
const ICON_PATHS = {
  pdf: "M340-260h280v-40H340v40Zm0-160h280v-40H340v40Zm-75.38 300q-27.62 0-46.12-18.5Q200-157 200-184.62v-590.76q0-27.62 18.5-46.12Q237-840 264.62-840H580l180 180v475.38q0 27.62-18.5 46.12Q723-120 695.38-120H264.62ZM560-640v-160H264.62q-9.24 0-16.93 7.69-7.69 7.69-7.69 16.93v590.76q0 9.24 7.69 16.93 7.69 7.69 16.93 7.69h430.76q9.24 0 16.93-7.69 7.69-7.69 7.69-16.93V-640H560ZM240-800v160-160 640-640Z",
  table: "M224.62-160q-27.62 0-46.12-18.5Q160-197 160-224.62v-510.76q0-27.62 18.5-46.12Q197-800 224.62-800h510.76q27.62 0 46.12 18.5Q800-763 800-735.38v510.76q0 27.62-18.5 46.12Q763-160 735.38-160H224.62ZM460-383.85H200v159.23q0 10.77 6.92 17.7 6.93 6.92 17.7 6.92H460v-183.85Zm40 0V-200h235.38q10.77 0 17.7-6.92 6.92-6.93 6.92-17.7v-159.23H500Zm-40-40v-185.38H200v185.38h260Zm40 0h260v-185.38H500v185.38ZM200-649.23h560v-86.15q0-10.77-6.92-17.7-6.93-6.92-17.7-6.92H224.62q-10.77 0-17.7 6.92-6.92 6.93-6.92 17.7v86.15Z",
  image: "M224.62-160q-27.62 0-46.12-18.5Q160-197 160-224.62v-510.76q0-27.62 18.5-46.12Q197-800 224.62-800h510.76q27.62 0 46.12 18.5Q800-763 800-735.38v510.76q0 27.62-18.5 46.12Q763-160 735.38-160H224.62Zm0-40h510.76q9.24 0 16.93-7.69 7.69-7.69 7.69-16.93v-510.76q0-9.24-7.69-16.93-7.69-7.69-16.93-7.69H224.62q-9.24 0-16.93 7.69-7.69 7.69-7.69 16.93v510.76q0 9.24 7.69 16.93 7.69 7.69 16.93 7.69ZM300-300h366.15L553.08-450.77 448.46-318.46l-70-84.62L300-300ZM200-200v-560 560Z",
  folder: "M184.62-200q-27.62 0-46.12-18.5Q120-237 120-264.62v-430.76q0-27.62 18.5-46.12Q157-760 184.62-760h199.23l80 80h311.53q27.62 0 46.12 18.5Q840-643 840-615.38v350.76q0 27.62-18.5 46.12Q803-200 775.38-200H184.62Zm0-40h590.76q10.77 0 17.7-6.92 6.92-6.93 6.92-17.7v-350.76q0-10.77-6.92-17.7-6.93-6.92-17.7-6.92H447.77l-80-80H184.62q-10.77 0-17.7 6.92-6.92 6.93-6.92 17.7v430.76q0 10.77 6.92 17.7 6.93 6.92 17.7 6.92ZM160-240v-480 480Z",
  slide: "M521-120q-53.85 0-137.69-14.58-83.85-14.57-132.46-51.73-27.7-21.46-58-67-30.31-45.54-55.5-98.54-25.2-53-41.27-106Q80-510.85 80-548q0-71.92 53.38-118.12 53.39-46.19 110.93-83.8 55.38-35.93 123-63Q434.92-840 506-840q68.77 0 128.81 28.46 60.04 28.46 116.88 67.62 13.46 9.23 35.16 27.69 21.69 18.46 42.5 45.77 20.8 27.31 35.61 62.42 14.81 35.12 15.04 77.89.46 66.3-29.73 138.73-30.19 72.42-79.92 132.77-49.73 60.34-114.66 99.5Q590.77-120 521-120Zm-1-40q60.46 0 118.19-36t102.35-90.73q44.61-54.73 71.88-120.23 27.27-65.5 27.58-123.5.31-59.77-34.46-105.31T729-710q-51-35-105.77-62.5Q568.46-800 506-800q-65.23 0-129.62 26.38Q312-747.23 260-713q-40.54 26.77-90.77 69.19Q119-601.38 119-552q0 32 15.5 81.23 15.5 49.23 39 99.62 23.5 50.38 50.5 92.88 27 42.5 50 60.27 36 26.23 111 42.12Q460-160 520-160ZM414-260q44.77 0 74.38-31.85Q518-323.69 518-364q0-18.15-7.08-35.81-7.07-17.65-24-32.57-24.3-22.31-39.84-48.62-15.54-26.31-23.31-58.38-8.46-37.08-36.96-58.85Q358.31-620 324-620q-40.54 0-72.27 31.73Q220-556.54 220-516q0 35.15 14.58 79.69 14.57 44.54 40.88 83.46 26.31 38.93 61.85 65.89Q372.85-260 414-260Zm0-40q-29.31 0-56.77-24.42-27.46-24.43-49.04-58.5-21.57-34.08-34.88-71.04Q260-490.92 260-516q0-23.92 20.04-43.96T324-580q17.38 0 36.04 11.96 18.65 11.96 25.19 37.42 9.46 38.93 26.81 69.74 17.34 30.8 47.04 58.26 9.84 8.85 14.38 19.31Q478-372.85 478-362q0 22.92-17.38 42.46Q443.23-300 414-300Zm236-160q8.54 0 14.27-5.73T670-480v-42.31l39.23 19.62q7.31 4.15 15.5 1.96 8.19-2.19 12.35-9.27 4.38-7.08 2.69-15.5t-9-11.81L685.38-560l45.39-22.69q7.31-4.16 8.11-12.08.81-7.92-2.57-15.23-4.16-7.08-11.96-8.38-7.81-1.31-15.12 2.07L670-596.69V-640q0-8.54-5.73-14.27T650-660q-8.54 0-14.27 5.73T630-640v44.85l-39-21.16q-7.08-4.15-15.38-1.96-8.31 2.19-12.7 9.27-3.38 6.31-2.19 14.85 1.19 8.53 8.73 11.69l42.08 18.61-39.77 26.77q-7.08 4.39-9.65 12.2-2.58 7.8 1.57 14.88 4.16 7.31 12.19 9.38 8.04 2.08 15.12-2.07l39-21.16V-480q0 8.54 5.73 14.27T650-460Zm-169.54-20Z",
  file: "M264.62-120q-27.62 0-46.12-18.5Q200-157 200-184.62v-590.76q0-27.62 18.5-46.12Q237-840 264.62-840H580l180 180v475.38q0 27.62-18.5 46.12Q723-120 695.38-120H264.62ZM560-640v-160H264.62q-9.24 0-16.93 7.69-7.69 7.69-7.69 16.93v590.76q0 9.24 7.69 16.93 7.69 7.69 16.93 7.69h430.76q9.24 0 16.93-7.69 7.69-7.69 7.69-16.93V-640H560ZM240-800v160-160 640-640Z",
};

const EXTENSION_ICONS = {
  pdf: ["pdf"],
  table: ["csv", "tsv", "xls", "xlsx", "xlsm", "ods", "parquet", "feather"],
  image: ["png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico", "heic"],
  slide: ["svs", "tif", "tiff", "ndpi", "scn", "mrxs", "vms", "vmu", "bif"], // tiled whole slide images
};
const ICON_BY_EXTENSION = new Map(
  Object.entries(EXTENSION_ICONS).flatMap(([icon, extensions]) => extensions.map((extension) => [extension, icon])),
);

function iconNameFor(entry) {
  if (entry.kind === "dir") return "folder";
  const dot = entry.name.lastIndexOf(".");
  return (dot > 0 && ICON_BY_EXTENSION.get(entry.name.slice(dot + 1).toLowerCase())) || "file";
}

function createIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 -960 960 960");
  svg.setAttribute("fill", "currentColor");
  svg.classList.add("icon");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICON_PATHS[name]);
  svg.append(path);
  return svg;
}

function showError(text) {
  message.textContent = text;
  message.hidden = !text;
}

async function load(path) {
  try {
    const listing = await salus.http.json("GET", "/list", { query: path ? { path } : {} });
    current = listing;
    pathInput.value = listing.path;
    upButton.disabled = listing.parent === null;
    showError("");
    render(listing);
  } catch (error) {
    // Stay on the directory that is shown; only say why the move failed.
    showError(error.body || error.message);
    if (current) pathInput.value = current.path;
  }
}

function render(listing) {
  list.replaceChildren(...listing.entries.map((entry) => {
    const item = document.createElement("li");
    const icon = createIcon(iconNameFor(entry));
    const name = Object.assign(document.createElement("span"), { className: "name", textContent: entry.name, title: entry.name });
    const size = Object.assign(document.createElement("span"), { className: "size", textContent: formatSize(entry.size) });
    item.append(icon, name, size);

    const full = joinPath(listing.path, entry.name);
    item.addEventListener("click", () => {
      list.querySelector(".selected")?.classList.remove("selected");
      item.classList.add("selected");
    });
    item.addEventListener("dblclick", async () => {
      if (entry.kind === "dir") return load(full);
      try { await salus.openFile(full); } catch (error) { showError(error.message); }
    });
    return item;
  }));
  list.scrollTop = 0;
}

upButton.addEventListener("click", () => current?.parent && load(current.parent));
document.getElementById("home").addEventListener("click", () => load(null));
document.getElementById("refresh").addEventListener("click", () => load(current?.path));
pathInput.addEventListener("keydown", (event) => { if (event.key === "Enter") load(pathInput.value.trim()); });

load(null);
