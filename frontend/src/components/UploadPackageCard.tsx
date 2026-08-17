import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "./ui/Button";
import { Card, CardBody, CardHeader } from "./ui/Card";
import { queryKeys } from "@/lib/queries";
import { API_BASE } from "@/config";
import type { Channel } from "@/lib/types";

interface PerFileResult {
  filename: string;
  status?: "stored" | "error";
  subdir?: string;
  size?: number;
  name?: string;
  version?: string;
  build?: string;
  error?: string;
}

type Phase = "idle" | "uploading" | "done" | "error";

interface UploadState {
  phase: Phase;
  loaded: number;
  total: number;
  message?: string;
  results?: PerFileResult[];
}

const INITIAL_STATE: UploadState = { phase: "idle", loaded: 0, total: 0 };

/**
 * Admin-only multi-file uploader for a non-mirror channel. Sends every
 * selected file under the same ``files`` form field; the backend extracts
 * each archive's subdir via rattler and files them into storage
 * accordingly — the user doesn't pick a subdir.
 *
 * Progress is aggregate across the whole request (XHR only exposes a
 * single ProgressEvent stream per upload). Per-file outcomes come back in
 * the JSON response and render as a status list below the dropzone.
 */
export function UploadPackageCard({ channel }: { channel: Channel }) {
  const qc = useQueryClient();

  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [state, setState] = useState<UploadState>(INITIAL_STATE);
  const inputRef = useRef<HTMLInputElement>(null);

  const canWrite =
    channel.my_role === "writer" ||
    channel.my_role === "owner" ||
    channel.my_role === "admin";
  if (!canWrite || channel.mirror_url) return null;

  const reset = () => {
    setFiles([]);
    setState(INITIAL_STATE);
    if (inputRef.current) inputRef.current.value = "";
  };

  const addFiles = (incoming: FileList | File[] | null) => {
    if (!incoming) return;
    const arr = Array.from(incoming);
    setFiles((prev) => {
      const seen = new Set(prev.map((f) => f.name));
      // Dedup by name — most browsers let you re-add the same file.
      return [...prev, ...arr.filter((f) => !seen.has(f.name))];
    });
    setState(INITIAL_STATE);
  };

  const removeFile = (name: string) => {
    setFiles((prev) => prev.filter((f) => f.name !== name));
  };

  const totalBytes = files.reduce((acc, f) => acc + f.size, 0);

  const upload = () => {
    if (files.length === 0) return;
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);

    setState({ phase: "uploading", loaded: 0, total: totalBytes });

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/channels/${encodeURIComponent(channel.name)}/packages`);
    xhr.withCredentials = true;

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        setState((s) => ({ ...s, loaded: e.loaded, total: e.total }));
      }
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        let results: PerFileResult[] = [];
        try {
          const parsed = JSON.parse(xhr.responseText);
          results = Array.isArray(parsed?.results) ? parsed.results : [];
        } catch {
          // Response wasn't JSON — stay generic.
        }
        const anyError = results.some((r) => r.status === "error");
        const anyStored = results.some((r) => r.status === "stored");
        setState({
          phase: anyStored && !anyError ? "done" : anyError && anyStored ? "done" : "error",
          loaded: totalBytes,
          total: totalBytes,
          results,
          message: anyError && !anyStored ? "All uploads failed." : undefined,
        });
        if (anyStored) {
          qc.invalidateQueries({ queryKey: queryKeys.packages(channel.name) });
        }
      } else {
        let detail = xhr.statusText;
        try {
          const parsed = JSON.parse(xhr.responseText);
          if (typeof parsed?.detail === "string") detail = parsed.detail;
        } catch {
          // not JSON
        }
        setState({
          phase: "error",
          loaded: 0,
          total: totalBytes,
          message: `${xhr.status}: ${detail}`,
        });
      }
    });

    xhr.addEventListener("error", () =>
      setState({ phase: "error", loaded: 0, total: totalBytes, message: "network error" }),
    );
    xhr.addEventListener("abort", () =>
      setState({ phase: "error", loaded: 0, total: totalBytes, message: "upload aborted" }),
    );

    xhr.send(form);
  };

  const pct = state.total > 0 ? Math.min(100, Math.round((state.loaded / state.total) * 100)) : 0;

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Upload packages</h2>
        <p className="mt-0.5 text-xs text-slate-600 dark:text-slate-400">
          Admin-only. Drop one or more <code>.conda</code> / <code>.tar.bz2</code> files — the
          subdir is read from each archive&apos;s <code>info/index.json</code>. A background
          reindex runs once after any file lands.
        </p>
      </CardHeader>
      <CardBody className="space-y-4">
        <label
          onDragEnter={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            addFiles(e.dataTransfer.files);
          }}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-6 text-center text-sm transition ${
            dragging
              ? "border-brand-500 bg-brand-50 text-brand-900 dark:border-brand-400 dark:bg-brand-500/10 dark:text-brand-200"
              : "border-slate-300 bg-slate-50 text-slate-600 hover:border-slate-400 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-400 dark:hover:border-slate-600"
          }`}
        >
          <input
            ref={inputRef}
            type="file"
            multiple
            accept=".conda,.tar.bz2"
            className="hidden"
            onChange={(e) => addFiles(e.target.files)}
          />
          <span className="font-medium">Drop packages here</span>
          <span className="mt-1 text-xs">or click to choose files</span>
        </label>

        {files.length > 0 && (
          <div className="space-y-1 rounded-md border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/40">
            {files.map((f) => (
              <div
                key={f.name}
                className="flex items-center justify-between gap-3 px-3 py-1.5 text-xs"
              >
                <span className="truncate font-mono text-slate-800 dark:text-slate-200">{f.name}</span>
                <span className="shrink-0 text-slate-500 dark:text-slate-400">
                  {(f.size / (1024 * 1024)).toFixed(2)} MB
                </span>
                <button
                  type="button"
                  onClick={() => removeFile(f.name)}
                  disabled={state.phase === "uploading"}
                  className="shrink-0 cursor-pointer text-slate-400 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-40 dark:text-slate-500 dark:hover:text-red-400"
                  aria-label={`Remove ${f.name}`}
                >
                  ✕
                </button>
              </div>
            ))}
            <div className="border-t border-slate-100 px-3 py-1.5 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
              {files.length} file{files.length === 1 ? "" : "s"} · {(totalBytes / (1024 * 1024)).toFixed(2)} MB
            </div>
          </div>
        )}

        <div className="flex gap-2">
          <Button
            variant="primary"
            disabled={files.length === 0 || state.phase === "uploading"}
            loading={state.phase === "uploading"}
            onClick={upload}
          >
            Upload {files.length > 0 ? `(${files.length})` : ""}
          </Button>
          {(files.length > 0 || state.phase !== "idle") && (
            <Button
              variant="secondary"
              onClick={reset}
              disabled={state.phase === "uploading"}
            >
              Clear
            </Button>
          )}
        </div>

        {state.phase === "uploading" && (
          <div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
              <div
                className="h-full bg-brand-500 transition-all"
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="mt-1 text-xs text-slate-600 dark:text-slate-400">
              {pct}% — {(state.loaded / (1024 * 1024)).toFixed(1)} / {(state.total / (1024 * 1024)).toFixed(1)} MB
            </div>
          </div>
        )}

        {state.results && state.results.length > 0 && (
          <div className="divide-y divide-slate-100 rounded-md border border-slate-200 dark:divide-slate-800 dark:border-slate-800">
            {state.results.map((r) => (
              <div key={r.filename} className="flex items-start justify-between gap-3 px-3 py-2 text-xs">
                <div className="min-w-0">
                  <div className="truncate font-mono text-slate-800 dark:text-slate-200">{r.filename}</div>
                  {r.status === "stored" ? (
                    <div className="text-slate-500 dark:text-slate-400">
                      → <code>{r.subdir}</code> · {r.name} {r.version}
                    </div>
                  ) : (
                    <div className="text-red-700 dark:text-red-400">{r.error}</div>
                  )}
                </div>
                <span
                  className={
                    r.status === "stored"
                      ? "shrink-0 rounded bg-brand-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200"
                      : "shrink-0 rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-red-800 dark:bg-red-500/15 dark:text-red-300"
                  }
                >
                  {r.status ?? "error"}
                </span>
              </div>
            ))}
          </div>
        )}

        {state.phase === "error" && state.message && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            {state.message}
          </div>
        )}
      </CardBody>
    </Card>
  );
}
