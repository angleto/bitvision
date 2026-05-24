// HTML5 drag-drop helpers for the Drive-style fascicolo UI.
//
// Two kinds of payloads travel over `dataTransfer`:
//   1. internal moves — a (possibly multi-select) batch of {kind,id,patient_id}
//      items serialized under MIME_TYPE, used to re-home items between folders.
//   2. OS file drops — plain `Files`/`items` from the user's desktop, walked
//      recursively so that dropping a directory uploads its whole subtree.
//
// Keeping both behaviors in one module lets the tree and the content pane
// share the same drop handlers without duplicating fragile dataTransfer code.

/** MIME type used to mark our own drag payloads. */
export const MIME_TYPE = "application/x-bvphoenix-item";

/** What kinds of things can be dragged between folders. */
export type DraggableKind = "study" | "series" | "folder" | "report" | "document";

export interface DraggableItem {
  kind: DraggableKind;
  id: string;
  patient_id: string;
  /** Optional current parent — useful for no-op detection on the same folder. */
  parent_folder_id?: string | null;
}

/** Shape stored in dataTransfer: always a list so multi-select is natural. */
export interface DragPayload {
  items: DraggableItem[];
}

// --- serialize / deserialize --------------------------------------------------

/**
 * Attach `items` to `dataTransfer` under our MIME type. Also writes a plain-text
 * fallback so external drop targets (logs, text editors) show something human.
 */
export function serializeItems(
  dataTransfer: DataTransfer,
  items: DraggableItem[],
  effect: DataTransfer["effectAllowed"] = "move",
): void {
  const payload: DragPayload = { items };
  const json = JSON.stringify(payload);
  dataTransfer.setData(MIME_TYPE, json);
  // Browsers normalize "text/plain" so a readable fallback is friendlier than raw JSON.
  const labels = items.map((i) => `${i.kind}:${i.id}`).join(", ");
  dataTransfer.setData("text/plain", labels);
  dataTransfer.effectAllowed = effect;
}

/** Convenience wrapper for single-item drags. */
export function serializeItem(dataTransfer: DataTransfer, item: DraggableItem): void {
  serializeItems(dataTransfer, [item]);
}

/**
 * Parse a drag payload out of `dataTransfer`. Returns null if the payload is
 * missing, malformed, or empty — callers should treat null as "not ours".
 */
export function deserializeItems(dataTransfer: DataTransfer): DraggableItem[] | null {
  const raw = dataTransfer.getData(MIME_TYPE);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as DragPayload;
    if (!parsed || !Array.isArray(parsed.items) || parsed.items.length === 0) return null;
    // Light runtime shape check — be forgiving, drop garbage entries.
    const clean = parsed.items.filter(
      (x): x is DraggableItem =>
        !!x &&
        typeof x.id === "string" &&
        typeof x.kind === "string" &&
        typeof x.patient_id === "string",
    );
    return clean.length ? clean : null;
  } catch {
    return null;
  }
}

/**
 * True iff the current drag contains our MIME type. Use from `onDragOver` to
 * decide whether to `preventDefault` and show the highlight.
 */
export function hasInternalPayload(dataTransfer: DataTransfer): boolean {
  // In dragover, values are not readable but types are — always check types.
  return Array.from(dataTransfer.types).includes(MIME_TYPE);
}

/** True iff the drag originates from the OS (has file entries). */
export function hasFiles(dataTransfer: DataTransfer): boolean {
  return Array.from(dataTransfer.types).includes("Files");
}

// --- OS file extraction -------------------------------------------------------

/**
 * A plucked file, tagged with its relative path so uploaders can preserve
 * directory structure (e.g. `case-42/CT/001.dcm`).
 */
export interface ExtractedFile {
  file: File;
  relativePath: string;
}

export interface ExtractFilesCallbacks {
  onFile?: (file: ExtractedFile) => void;
  /** Invoked once per encountered directory, with its relative path. */
  onFolder?: (relativePath: string) => void;
}

/**
 * Walk `DataTransferItemList` recursively and emit every file inside it,
 * preserving directory paths. Falls back to the flat `dataTransfer.files` list
 * when `webkitGetAsEntry` is unavailable (older Safari / non-browser envs).
 *
 * Returns a flat array when traversal finishes, for callers that want it.
 */
export async function extractFiles(
  dataTransfer: DataTransfer,
  callbacks: ExtractFilesCallbacks = {},
): Promise<ExtractedFile[]> {
  const out: ExtractedFile[] = [];
  const emit = (f: ExtractedFile) => {
    out.push(f);
    callbacks.onFile?.(f);
  };

  // Snapshot entries synchronously — `items` gets neutered after the event loop turn.
  const entries: FileSystemEntry[] = [];
  const looseFiles: File[] = [];
  const items = dataTransfer.items;
  if (items && items.length > 0) {
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (item.kind !== "file") continue;
      const entry =
        // webkit prefix is still the only thing Safari exposes; newer APIs use getAsEntry().
        (
          item as unknown as { webkitGetAsEntry?: () => FileSystemEntry | null }
        ).webkitGetAsEntry?.() ?? null;
      if (entry) entries.push(entry);
      else {
        const f = item.getAsFile();
        if (f) looseFiles.push(f);
      }
    }
  } else if (dataTransfer.files) {
    for (let i = 0; i < dataTransfer.files.length; i += 1) looseFiles.push(dataTransfer.files[i]);
  }

  for (const f of looseFiles) emit({ file: f, relativePath: f.name });
  for (const entry of entries) {
    await walkEntry(entry, "", emit, callbacks.onFolder);
  }
  return out;
}

async function walkEntry(
  entry: FileSystemEntry,
  parentPath: string,
  onFile: (f: ExtractedFile) => void,
  onFolder?: (relativePath: string) => void,
): Promise<void> {
  const here = parentPath ? `${parentPath}/${entry.name}` : entry.name;
  if (entry.isFile) {
    const file = await fileFromEntry(entry as FileSystemFileEntry);
    if (file) onFile({ file, relativePath: here });
    return;
  }
  if (entry.isDirectory) {
    onFolder?.(here);
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    // readEntries is paginated; keep calling until it returns empty.
    for (;;) {
      const batch = await readBatch(reader);
      if (batch.length === 0) break;
      for (const child of batch) await walkEntry(child, here, onFile, onFolder);
    }
  }
}

function fileFromEntry(entry: FileSystemFileEntry): Promise<File | null> {
  return new Promise((resolve) => {
    entry.file(
      (f) => resolve(f),
      () => resolve(null),
    );
  });
}

function readBatch(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve) => {
    reader.readEntries(
      (batch) => resolve(Array.from(batch)),
      () => resolve([]),
    );
  });
}
