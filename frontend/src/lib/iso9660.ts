// Minimal ISO 9660 + Joliet reader — extracts files from a clinical DVD
// ISO image without leaving the browser. The bulk-upload pipeline picks
// up the extracted blobs as if the user had dragged the unpacked
// folder, so existing magic-byte classification + DICOMDIR detection
// keeps working.
//
// Scope:
//   - Plain ISO 9660 (Type 1 Primary Volume Descriptor).
//   - Joliet SVD (Type 2 Supplementary Volume Descriptor) for Unicode
//     long filenames, preferred when present — 99% of clinical DVDs
//     ship a Joliet SVD alongside the PVD so REFERTO.PDF stays
//     "REFERTO.PDF" instead of being truncated to 8.3 form.
//   - No Rock Ridge, no UDF (the ISO 9660 reader covers the radiology
//     DVD use case; UDF support is left as a follow-up if a real
//     hospital DVD turns out to be UDF-only).
//
// Public API:
//   isLikelyIsoFile(file)  — quick sniff on the standard CD001 magic.
//   extractIsoFiles(file)  — async generator yielding {path, file}.
//
// The generator shape is deliberate: a 4 GB DVD has thousands of
// instances; we want the uploader to start classifying / batching
// while extraction is still in progress, not buffer everything.

const SECTOR_SIZE = 2048;
const SYSTEM_AREA_SECTORS = 16;

const VD_TYPE_BOOT = 0;
const VD_TYPE_PRIMARY = 1;
const VD_TYPE_SUPPLEMENTARY = 2;
const VD_TYPE_TERMINATOR = 255;

// Directory record file_flags bits.
const FLAG_DIRECTORY = 0x02;

interface VolumeDescriptor {
  type: number;
  rootRecord: DataView;
  /** Joliet SVD escape sequences indicate UCS-2 encoding. */
  joliet: boolean;
}

interface DirectoryRecord {
  lba: number;
  size: number;
  isDirectory: boolean;
  name: string;
}

/**
 * Sniff the standard CD001 magic at byte 32769 (sector 16, offset 1).
 * Cheap enough to do on a small slice; we read 6 bytes and bail.
 */
export async function isLikelyIsoFile(file: File): Promise<boolean> {
  if (file.size < SYSTEM_AREA_SECTORS * SECTOR_SIZE + 6) return false;
  const slice = await file
    .slice(SYSTEM_AREA_SECTORS * SECTOR_SIZE + 1, SYSTEM_AREA_SECTORS * SECTOR_SIZE + 6)
    .arrayBuffer();
  const buf = new Uint8Array(slice);
  // "CD001"
  return (
    buf[0] === 0x43 && buf[1] === 0x44 && buf[2] === 0x30 && buf[3] === 0x30 && buf[4] === 0x31
  );
}

/**
 * Yield ``{path, file}`` for every regular file in the ISO. ``path``
 * uses forward slashes; the leading directory is the ISO root (no
 * leading slash).
 */
export async function* extractIsoFiles(iso: File): AsyncGenerator<{ path: string; file: File }> {
  const vd = await pickVolumeDescriptor(iso);
  if (vd === null) {
    throw new Error("not a recognisable ISO 9660 image");
  }

  // Walk the root directory tree breadth-first. Each entry yields
  // either further DirectoryRecord rows (recurse) or a regular file
  // (slice + emit).
  type Frame = { record: DataView; pathPrefix: string };
  const queue: Frame[] = [{ record: vd.rootRecord, pathPrefix: "" }];

  while (queue.length > 0) {
    const frame = queue.shift();
    if (!frame) break;
    const root = frame.record;
    const lba = readLba(root);
    const size = readSize(root);
    const sectorCount = Math.ceil(size / SECTOR_SIZE);

    for (let i = 0; i < sectorCount; i++) {
      const sectorOffset = (lba + i) * SECTOR_SIZE;
      const sectorEnd = Math.min(sectorOffset + SECTOR_SIZE, iso.size);
      const sectorBytes = await iso.slice(sectorOffset, sectorEnd).arrayBuffer();
      const sector = new DataView(sectorBytes);
      let cursor = 0;
      while (cursor < sector.byteLength) {
        const recLen = sector.getUint8(cursor);
        if (recLen === 0) {
          // Padding to sector boundary — directory records never
          // straddle sectors. Move on.
          break;
        }
        const recView = new DataView(
          sectorBytes,
          cursor,
          Math.min(recLen, sector.byteLength - cursor),
        );
        const rec = parseDirectoryRecord(recView, vd.joliet);
        // ``.`` (self) and ``..`` (parent) entries have a 1-byte name
        // (0x00 / 0x01) — skip them.
        if (rec.name === "." || rec.name === "..") {
          cursor += recLen;
          continue;
        }
        if (rec.isDirectory) {
          queue.push({
            record: recView,
            pathPrefix: `${frame.pathPrefix + rec.name}/`,
          });
        } else {
          const fileEnd = rec.lba * SECTOR_SIZE + rec.size;
          if (rec.size > 0) {
            const blob = iso.slice(rec.lba * SECTOR_SIZE, fileEnd);
            const path = frame.pathPrefix + rec.name;
            yield {
              path,
              file: new File([blob], rec.name, {
                type: blob.type || "application/octet-stream",
              }),
            };
          }
        }
        cursor += recLen;
      }
    }
  }
}

async function pickVolumeDescriptor(iso: File): Promise<VolumeDescriptor | null> {
  // Walk volume descriptors starting at sector 16 until we hit the
  // terminator. Prefer the Joliet SVD over the PVD for filenames.
  let primary: VolumeDescriptor | null = null;
  let joliet: VolumeDescriptor | null = null;

  for (let sector = SYSTEM_AREA_SECTORS; sector < SYSTEM_AREA_SECTORS + 16; sector++) {
    const offset = sector * SECTOR_SIZE;
    if (offset + SECTOR_SIZE > iso.size) break;
    const buf = await iso.slice(offset, offset + SECTOR_SIZE).arrayBuffer();
    const view = new DataView(buf);
    const type = view.getUint8(0);
    // Identifier "CD001" at bytes 1..6
    const id = String.fromCharCode(
      view.getUint8(1),
      view.getUint8(2),
      view.getUint8(3),
      view.getUint8(4),
      view.getUint8(5),
    );
    if (id !== "CD001") return null;
    if (type === VD_TYPE_TERMINATOR) break;
    if (type === VD_TYPE_PRIMARY) {
      // Root directory record lives at byte offset 156, length 34.
      primary = {
        type,
        rootRecord: new DataView(buf, 156, 34),
        joliet: false,
      };
    } else if (type === VD_TYPE_SUPPLEMENTARY) {
      // Escape sequences at bytes 88..120. Joliet UCS-2 levels are
      // identified by "%/@" (Level 1), "%/C" (Level 2), "%/E" (Level 3).
      const esc = new Uint8Array(buf, 88, 32);
      const isJoliet =
        esc[0] === 0x25 &&
        esc[1] === 0x2f &&
        (esc[2] === 0x40 || esc[2] === 0x43 || esc[2] === 0x45);
      if (isJoliet) {
        joliet = {
          type,
          rootRecord: new DataView(buf, 156, 34),
          joliet: true,
        };
      }
    } else if (type === VD_TYPE_BOOT) {
      // Skip — boot record metadata is irrelevant here.
    }
  }
  return joliet ?? primary;
}

function readLba(record: DataView): number {
  // bytes 2..6 — LBA in little-endian. (bytes 6..10 mirror it big-endian.)
  return record.getUint32(2, true);
}

function readSize(record: DataView): number {
  // bytes 10..14 — data length in little-endian.
  return record.getUint32(10, true);
}

function parseDirectoryRecord(rec: DataView, joliet: boolean): DirectoryRecord {
  const lba = readLba(rec);
  const size = readSize(rec);
  const fileFlags = rec.getUint8(25);
  const isDirectory = (fileFlags & FLAG_DIRECTORY) !== 0;
  const fidLen = rec.getUint8(32);
  const nameBytes = new Uint8Array(rec.buffer, rec.byteOffset + 33, fidLen);

  let name: string;
  if (fidLen === 1 && (nameBytes[0] === 0x00 || nameBytes[0] === 0x01)) {
    name = nameBytes[0] === 0x00 ? "." : "..";
  } else if (joliet) {
    // UCS-2 big-endian. fidLen is in bytes; we step through pairs.
    const chars: number[] = [];
    for (let i = 0; i + 1 < nameBytes.length; i += 2) {
      const code = (nameBytes[i] << 8) | nameBytes[i + 1];
      chars.push(code);
    }
    name = String.fromCharCode(...chars);
  } else {
    name = String.fromCharCode(...nameBytes);
  }

  // Strip the ISO 9660 file version suffix (";1") from leaf names.
  // Directories never carry one.
  if (!isDirectory) {
    const semi = name.lastIndexOf(";");
    if (semi >= 0) name = name.slice(0, semi);
    // Trim trailing dot (8.3 names sometimes carry "FILE.")
    if (name.endsWith(".")) name = name.slice(0, -1);
  }

  return { lba, size, isDirectory, name };
}
