import { message } from "../shared/format";
import { salus } from "../shared/salus";

/** Asks Salus to open a file in a viewer. */
export function openPath(path: string): void {
  salus.openFile(path).catch((error) => window.alert(message(error)));
}

export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // The clipboard API can be refused inside a frame; fall back to the old way.
    const area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
}
