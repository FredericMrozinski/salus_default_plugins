import { api, encode } from "../shared/salus";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Uploads one file in chunks (design 6.5): start, PUT chunks at their offset (resyncing when the backend
 * says how much it has), commit. Chunks are retried with a growing pause.
 */
export async function uploadFile(
  job: { file: File; path: string },
  source: string,
  overwrite: boolean,
  onProgress: (sent: number) => void,
  signal: AbortSignal,
): Promise<void> {
  const { file, path } = job;
  const start = await api<{ upload_id: string; chunk_size: number }>("POST", "/upload", {
    json: { source, path, size: file.size, overwrite },
  });
  const base = `/upload/${encode(start.upload_id)}`;
  try {
    let offset = 0;
    let failures = 0; // in a row, without progress
    while (offset < file.size) {
      if (signal.aborted) throw new Error("cancelled");
      const chunk = file.slice(offset, offset + start.chunk_size);
      try {
        const reply = await api<{ received: number; resync?: boolean }>("PUT", `${base}/${offset}`, { body: chunk, signal });
        offset = reply.resync ? reply.received : offset + chunk.size;
        failures = 0;
      } catch (error) {
        if (signal.aborted || ++failures > 4) throw error;
        await sleep(500 * 2 ** failures);
        offset = (await api<{ received: number }>("GET", base)).received; // continue from what the backend has
      }
      onProgress(offset);
    }
    await api("POST", `${base}/commit`);
    onProgress(file.size);
  } catch (error) {
    api("DELETE", base).catch(() => undefined);
    throw error;
  }
}

/** A file to upload and its path relative to the destination folder (`cohort/sub/a.svs` for a file in a folder). */
export type Picked = { file: File; rel: string };

const isHidden = (rel: string) => rel.split("/").some((part) => part.startsWith("."));

/** From a file chooser (`webkitRelativePath` is set when a folder was chosen). Hidden files are left out. */
export function fromFileList(list: FileList): { picked: Picked[]; skipped: number } {
  const all = Array.from(list).map((file) => ({ file, rel: file.webkitRelativePath || file.name }));
  const picked = all.filter((p) => !isHidden(p.rel));
  return { picked, skipped: all.length - picked.length };
}

const readAll = (reader: FileSystemDirectoryReader) =>
  new Promise<FileSystemEntry[]>((resolve, reject) => {
    const entries: FileSystemEntry[] = [];
    const next = () =>
      reader.readEntries((batch) => {
        if (batch.length === 0) resolve(entries);
        else (entries.push(...batch), next()); // readEntries returns at most ~100 at a time
      }, reject);
    next();
  });

async function walk(entry: FileSystemEntry, prefix: string, out: Picked[], skipped: { n: number }): Promise<void> {
  if (entry.name.startsWith(".")) {
    skipped.n++;
    return;
  }
  const rel = prefix + entry.name;
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
    out.push({ file, rel });
  } else if (entry.isDirectory) {
    for (const child of await readAll((entry as FileSystemDirectoryEntry).createReader())) await walk(child, rel + "/", out, skipped);
  }
}

/** From a drop: files and folders (walked recursively). */
export async function fromDrop(items: DataTransferItemList): Promise<{ picked: Picked[]; skipped: number }> {
  // The entries and files must be taken before the first await: the list is emptied when the event handler returns.
  const taken = Array.from(items).filter((item) => item.kind === "file").map((item) => ({ entry: item.webkitGetAsEntry?.() ?? null, file: item.getAsFile() }));
  const picked: Picked[] = [];
  const skipped = { n: 0 };
  for (const { entry, file } of taken) {
    if (entry) await walk(entry, "", picked, skipped);
    else if (file && !file.name.startsWith(".")) picked.push({ file, rel: file.name });
    else skipped.n++;
  }
  return { picked, skipped: skipped.n };
}

/** What a drop or a chooser produced. */
export type Dropped = { picked: Picked[]; skipped: number };
