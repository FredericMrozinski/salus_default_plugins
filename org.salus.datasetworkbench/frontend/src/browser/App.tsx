import { useEffect, useState } from "react";
import { DatasetsTab } from "./DatasetsTab";
import { SourcesTab } from "./SourcesTab";
import { UploadPanel } from "./UploadPanel";
import { UploadProgress } from "./UploadProgress";

export function App() {
  // Targets that accept a file drag handle it themselves. For everything else the cursor says "not allowed", and the drop is
  // swallowed, because a file dropped on a page that ignores it would make the browser open the file and leave the page.
  useEffect(() => {
    const isFiles = (e: DragEvent) => !!e.dataTransfer && Array.from(e.dataTransfer.types).includes("Files");
    const over = (e: DragEvent) => {
      if (!isFiles(e) || e.defaultPrevented) return;
      e.preventDefault();
      e.dataTransfer!.dropEffect = "none";
    };
    const drop = (e: DragEvent) => isFiles(e) && e.preventDefault();
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
    };
  }, []);
  const [tab, setTab] = useState<"sources" | "datasets">("sources");
  return (
    <div className="app">
      <div className="tabs" role="tablist">
        <button role="tab" className={`tab ${tab === "sources" ? "active" : ""}`} onClick={() => setTab("sources")}>
          Sources
        </button>
        <button role="tab" className={`tab ${tab === "datasets" ? "active" : ""}`} onClick={() => setTab("datasets")}>
          Datasets
        </button>
      </div>
      {/* Both stay mounted so their expansion state survives a tab switch. */}
      <div className="pane" hidden={tab !== "sources"}>
        <SourcesTab />
      </div>
      <div className="pane" hidden={tab !== "datasets"}>
        <DatasetsTab />
      </div>
      {/* Docked at the bottom, full width: choosing what to upload, and the progress of what is uploading. */}
      <UploadPanel />
      <UploadProgress />
    </div>
  );
}
