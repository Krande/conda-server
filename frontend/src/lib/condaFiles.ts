// Client-side .conda archive inspector.
//
// The whole reason this lives in the browser rather than the server:
// listing files is a read-heavy operation the user opts into per-click,
// and the pod has better things to do than stream tens of megabytes
// from Garage just to hand back a path list. The browser already has
// authenticated access to the download URL, so we can do the whole
// thing locally.
//
// .conda is a ZIP of (info-*.tar.zst, pkg-*.tar.zst, metadata.json).
// We only need the info tar — it holds `info/paths.json`, which is
// the canonical enumeration of installed files with sizes + sha256s.
//
// fflate handles the outer ZIP; zstddec (a wasm wrapper around the
// reference zstd) decompresses the inner archive. The tar walk is ~40
// lines of hand-rolled header parsing, enough for the one member we need.
//
// We walk the input frame-by-frame and hand each frame to the decoder
// sliced exactly to its end, because the conda toolchain often emits
// info-*.tar.zst with trailing bytes that aren't part of the zstd
// stream (likely ZIP block padding). The decoder's size-strict API
// would otherwise fail those payloads with srcSize_wrong (-72).
//
// .tar.bz2 is intentionally out of scope — the format is deprecated
// and browser-side bzip2 costs more dep weight than it's worth.

import { strFromU8, unzipSync } from "fflate";
import { ZSTDDecoder } from "zstddec/stream";

// The wasm blob is ~250 KB; load it once per session and reuse for every
// archive the user opens. The dynamic import in PackageDetail already
// lazy-loads this module, so the wasm fetch only happens on first click.
let decoderPromise: Promise<ZSTDDecoder> | null = null;
function getZstdDecoder(): Promise<ZSTDDecoder> {
  if (!decoderPromise) {
    const d = new ZSTDDecoder();
    decoderPromise = d.init().then(() => d);
  }
  return decoderPromise;
}

const ZSTD_MAGIC = [0x28, 0xb5, 0x2f, 0xfd];

/** Walk one zstd frame starting at ``off`` and return the byte index
 *  immediately after it. Handles classic zstd frames only — skippable
 *  frames are passed through by length, since conda archives use them
 *  for nothing we care about.
 *
 *  Reference: RFC 8478 §3.1 (frame format) and §3.1.1.1 (block layout).
 */
function findZstdFrameEnd(buf: Uint8Array, off: number): number {
  if (buf.length - off < 6) {
    throw new Error("truncated zstd frame header");
  }
  // Skippable frame: magic 0x184D2A5? + 4-byte little-endian length.
  if (buf[off] >= 0x50 && buf[off] <= 0x5f &&
      buf[off + 1] === 0x2a && buf[off + 2] === 0x4d && buf[off + 3] === 0x18) {
    const len = buf[off + 4] | (buf[off + 5] << 8) | (buf[off + 6] << 16) | (buf[off + 7] << 24);
    return off + 8 + (len >>> 0);
  }
  if (buf[off] !== ZSTD_MAGIC[0] || buf[off + 1] !== ZSTD_MAGIC[1] ||
      buf[off + 2] !== ZSTD_MAGIC[2] || buf[off + 3] !== ZSTD_MAGIC[3]) {
    throw new Error("not a zstd frame");
  }
  let p = off + 4;
  const fhd = buf[p++];
  const fcsFlag = (fhd >> 6) & 0x3;
  const singleSegment = (fhd >> 5) & 0x1;
  const checksumFlag = (fhd >> 2) & 0x1;
  const didFlag = fhd & 0x3;
  if (!singleSegment) p += 1; // Window_Descriptor
  p += [0, 1, 2, 4][didFlag]; // Dictionary_ID
  // Frame_Content_Size: 0|2|4|8 bytes, or 1 when single-segment + fcsFlag=0.
  p += fcsFlag === 0 ? (singleSegment ? 1 : 0) : (1 << fcsFlag);
  // Blocks: 3-byte header { last:1, type:2, size:21 } + payload.
  while (p < buf.length) {
    if (buf.length - p < 3) throw new Error("truncated zstd block header");
    const bh = buf[p] | (buf[p + 1] << 8) | (buf[p + 2] << 16);
    p += 3;
    const lastBlock = bh & 0x1;
    const blockType = (bh >> 1) & 0x3;
    const blockSize = bh >> 3;
    if (blockType === 3) throw new Error("reserved zstd block type");
    p += blockType === 1 ? 1 : blockSize; // RLE blocks are always 1 byte.
    if (lastBlock) break;
  }
  if (checksumFlag) p += 4;
  return p;
}

/** Decompress a zstd stream that may include one or more frames plus
 *  trailing non-zstd bytes (ZIP padding, alignment, etc.). Stops at the
 *  first byte that doesn't look like a frame magic.
 */
async function decompressZstd(buf: Uint8Array): Promise<Uint8Array> {
  const decoder = await getZstdDecoder();
  const parts: Uint8Array[] = [];
  let total = 0;
  let off = 0;
  while (off < buf.length) {
    // Stop at the first byte that isn't a zstd or skippable-frame magic.
    const isZstd = buf[off] === ZSTD_MAGIC[0] && buf[off + 1] === ZSTD_MAGIC[1] &&
                   buf[off + 2] === ZSTD_MAGIC[2] && buf[off + 3] === ZSTD_MAGIC[3];
    const isSkippable = buf[off] >= 0x50 && buf[off] <= 0x5f &&
                        buf[off + 1] === 0x2a && buf[off + 2] === 0x4d && buf[off + 3] === 0x18;
    if (!isZstd && !isSkippable) break;
    const end = findZstdFrameEnd(buf, off);
    if (isZstd) {
      const out = decoder.decode(buf.subarray(off, end));
      parts.push(out);
      total += out.length;
    }
    off = end;
  }
  if (parts.length === 1) return parts[0];
  const merged = new Uint8Array(total);
  let cursor = 0;
  for (const p of parts) {
    merged.set(p, cursor);
    cursor += p.length;
  }
  return merged;
}

/** Raised when the archive fetch itself fails, before any parsing.
 *
 *  ``kind: "network"`` means ``fetch()`` threw a ``TypeError`` — the
 *  browser refused to hand us the response. This lumps together genuine
 *  network failures and CORS rejections, because browsers deliberately
 *  collapse the two into an indistinguishable ``TypeError: Failed to
 *  fetch`` (the CORS-specific detail is only ever printed to the
 *  devtools console, never exposed to JS). In this app the single most
 *  common cause is a missing CORS rule on the object-storage
 *  bucket/account: the download endpoint 302-redirects our same-origin
 *  fetch to a cross-origin presigned URL (S3 / Azure Blob / GCS), and
 *  without an allow rule for the site origin the browser blocks the
 *  response. The UI treats this kind as "probably CORS" and shows an
 *  actionable hint.
 *
 *  ``kind: "http"`` means the request completed but the server (or the
 *  storage host) returned a non-2xx status — a real, readable error.
 */
export class CondaFilesFetchError extends Error {
  readonly kind: "network" | "http";
  readonly status?: number;
  constructor(message: string, kind: "network" | "http", status?: number) {
    super(message);
    this.name = "CondaFilesFetchError";
    this.kind = kind;
    this.status = status;
  }
}

/** True when an error thrown by :func:`listCondaFiles` is a browser-level
 *  fetch failure (network or, most often in this app, a blocked
 *  cross-origin storage response). Uses a duck-typed ``kind`` check
 *  rather than ``instanceof`` so it survives the dynamic-import module
 *  boundary and any bundler class-identity quirks. */
export function isNetworkFetchError(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { kind?: unknown }).kind === "network"
  );
}

export interface CondaFileEntry {
  path: string;
  size: number | null;
  pathType?: string;
  sha256?: string;
}

export interface CondaRunExports {
  weak: string[];
  strong: string[];
  weakConstrains: string[];
  strongConstrains: string[];
  noarch: string[];
}

export interface CondaFilesResult {
  files: CondaFileEntry[];
  totalBytes: number;
  pathsVersion: number;
  // Absent when the archive doesn't ship a run_exports.json (older
  // builds, packages that have nothing to export, etc.). Not an error.
  runExports?: CondaRunExports;
}

/** Fetch and parse a .conda archive's info/paths.json.
 *
 *  ``credentials: "same-origin"`` and not "include" — the initial hop to
 *  our own server needs the session cookie (for private-channel ACL
 *  checks), but the follow-up 302 lands on the object-store host, which
 *  is cross-origin. S3 doesn't set ``Access-Control-Allow-Credentials:
 *  true`` on its responses, and the browser refuses to hand the body to
 *  JS if credentials mode is "include" without that header. The
 *  presigned URL already carries its auth in the query string, so
 *  sending cookies there would be wrong anyway.
 */
export async function listCondaFiles(url: string): Promise<CondaFilesResult> {
  // Start the wasm fetch in parallel with the archive download — first
  // call pays the wasm latency, subsequent calls hit a resolved promise.
  const decoderReady = getZstdDecoder();

  let resp: Response;
  try {
    resp = await fetch(url, { credentials: "same-origin" });
  } catch (err) {
    // fetch() only throws for network-level failures — DNS, connection
    // refused, or (the common one here) a cross-origin storage response
    // the browser blocked for want of a CORS rule. All arrive as an
    // opaque TypeError; see CondaFilesFetchError for why we can't tell
    // them apart and how the UI surfaces the CORS remedy.
    throw new CondaFilesFetchError(
      err instanceof Error ? err.message : String(err),
      "network",
    );
  }
  if (!resp.ok) {
    throw new CondaFilesFetchError(
      `download failed: ${resp.status} ${resp.statusText}`,
      "http",
      resp.status,
    );
  }
  const totalBytes = Number(resp.headers.get("content-length") ?? 0);
  const archiveBytes = new Uint8Array(await resp.arrayBuffer());

  const unzipped = unzipSync(archiveBytes);
  const infoMember = Object.keys(unzipped).find(
    (k) => k.startsWith("info-") && k.endsWith(".tar.zst"),
  );
  if (!infoMember) {
    throw new Error("no info-*.tar.zst member found — is this a valid .conda?");
  }

  await decoderReady;
  const tarBytes = await decompressZstd(unzipped[infoMember]);
  const pathsJsonBytes = extractTarMember(tarBytes, "info/paths.json");
  if (!pathsJsonBytes) {
    throw new Error("info/paths.json not present in the archive");
  }

  const parsed = JSON.parse(strFromU8(pathsJsonBytes));
  const files: CondaFileEntry[] = (parsed.paths ?? []).map((p: Record<string, unknown>) => ({
    path: (p._path as string | undefined) ?? "",
    size: (p.size_in_bytes as number | undefined) ?? null,
    pathType: p.path_type as string | undefined,
    sha256: p.sha256 as string | undefined,
  }));

  // info/run_exports.json is optional — older builds omit it, as do
  // packages that have nothing to export. strong_exports/weak_exports
  // are the recipe-level pinnings that propagate to downstream packages
  // that (build-)depend on this one. They live in the archive and are
  // NOT surfaced in repodata, so the browser is the only place we can
  // learn them without adding server-side parsing.
  const runExportsBytes = extractTarMember(tarBytes, "info/run_exports.json");
  let runExports: CondaRunExports | undefined;
  if (runExportsBytes) {
    try {
      const rx = JSON.parse(strFromU8(runExportsBytes)) as Record<string, unknown>;
      const asList = (v: unknown): string[] =>
        Array.isArray(v) ? (v as string[]).filter((s) => typeof s === "string") : [];
      runExports = {
        weak: asList(rx.weak),
        strong: asList(rx.strong),
        weakConstrains: asList(rx.weak_constrains),
        strongConstrains: asList(rx.strong_constrains),
        noarch: asList(rx.noarch),
      };
    } catch {
      // Malformed run_exports is non-fatal — just drop it.
    }
  }

  return {
    files,
    totalBytes: totalBytes > 0 ? totalBytes : archiveBytes.length,
    pathsVersion: (parsed.paths_version as number | undefined) ?? 1,
    runExports,
  };
}

/** Walk a tar archive's 512-byte header blocks to find one member by path.
 *  Classic ustar only — info/paths.json's path is short enough that we
 *  never hit GNU/PAX extended-header edge cases. Returns the member's
 *  payload bytes, or null if not present.
 */
function extractTarMember(tar: Uint8Array, target: string): Uint8Array | null {
  const decoder = new TextDecoder("utf-8", { fatal: false });
  let offset = 0;
  while (offset + 512 <= tar.length) {
    const nameEnd = indexOfNul(tar, offset, offset + 100);
    const name = decoder.decode(tar.subarray(offset, nameEnd));
    // Two consecutive zero blocks signal end-of-archive.
    if (!name) {
      offset += 512;
      continue;
    }
    // Size field: 11 octal digits + NUL at offset 124.
    const sizeEnd = indexOfNul(tar, offset + 124, offset + 135);
    const sizeStr = decoder.decode(tar.subarray(offset + 124, sizeEnd)).trim();
    const size = parseInt(sizeStr, 8);
    if (!Number.isFinite(size) || size < 0) {
      break;
    }
    if (name === target) {
      return tar.subarray(offset + 512, offset + 512 + size);
    }
    // Advance past this header + payload, rounded up to the next 512-byte block.
    const payload = size === 0 ? 0 : Math.ceil(size / 512) * 512;
    offset += 512 + payload;
  }
  return null;
}

function indexOfNul(bytes: Uint8Array, start: number, end: number): number {
  for (let i = start; i < end; i++) {
    if (bytes[i] === 0) return i;
  }
  return end;
}
