import { useSyncExternalStore } from "react";
import type { SourceNode } from "../shared/types";
import { Dropped, uploadFile } from "./upload";
import { createQueue } from "./queueCore";

export type { Job, JobStatus, Snapshot } from "./queueCore";

// The upload queue lives outside React, so uploads go on when the panels close. Two small views show it: the panel for
// choosing files (UploadPanel) and the progress bar (UploadProgress). The queue logic itself is in queueCore.ts.

let onBatchDone: (() => void) | null = null;

const queue = createQueue({
  upload: (job, signal, onProgress) => uploadFile(job, job.sourceId, job.overwrite, onProgress, signal),
  onBatchDone: () => onBatchDone?.(),
});

export const enqueue = queue.enqueue;
export const cancelAll = queue.cancelAll;
export const dismiss = queue.dismiss;
export const useUploads = () => useSyncExternalStore(queue.subscribe, queue.getSnapshot);

/** Called when a batch is over (refresh the views of what was uploaded). */
export function setOnBatchDone(handler: () => void): void {
  onBatchDone = handler;
}

// ---- the panel for choosing files: where it is open, and what was dropped on a tree item to open it

export type PanelTarget = { source: SourceNode; folder: string; initial?: Dropped; nonce: number };
let panel: PanelTarget | null = null;
const panelListeners = new Set<() => void>();
const emitPanel = () => panelListeners.forEach((l) => l());

export function openUploadPanel(target: { source: SourceNode; folder?: string; initial?: Dropped }): void {
  panel = { source: target.source, folder: target.folder ?? "", initial: target.initial, nonce: Date.now() + Math.random() };
  emitPanel();
}

export function closeUploadPanel(): void {
  panel = null;
  emitPanel();
}

export const useUploadPanel = (): PanelTarget | null =>
  useSyncExternalStore((l) => (panelListeners.add(l), () => void panelListeners.delete(l)), () => panel);
