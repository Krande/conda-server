import { useState } from "react";
import { Button } from "./ui/Button";
import { ErrorState } from "./ui/EmptyState";
import { Input } from "./ui/Input";
import { useCreateChannel } from "@/lib/queries";

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
      {children}
    </span>
  );
}

const checkboxCls =
  "rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-900";

/**
 * Create-channel form. Shared by the Admin dashboard and the Channels page so
 * there's a single source of truth for the create flow. `onCreated` fires with
 * the new channel name after a successful create; the caller decides what to
 * show (banner, navigate, collapse a panel, …).
 */
export function CreateChannelForm({
  onCreated,
  onCancel,
}: {
  onCreated?: (name: string) => void;
  onCancel?: () => void;
}) {
  const create = useCreateChannel();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [isPrivate, setIsPrivate] = useState(false);
  const [mirrorEnabled, setMirrorEnabled] = useState(false);
  const [mirrorUrl, setMirrorUrl] = useState("https://conda.anaconda.org/conda-forge");
  const [mirrorTtl, setMirrorTtl] = useState("900");

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    const channel = await create.mutateAsync({
      name: name.trim(),
      description: description.trim() || undefined,
      private: isPrivate,
      mirror_url: mirrorEnabled ? mirrorUrl.trim() : undefined,
      mirror_cache_seconds: mirrorEnabled ? Number(mirrorTtl) : undefined,
    });
    setName("");
    setDescription("");
    setIsPrivate(false);
    setMirrorEnabled(false);
    onCreated?.(channel.name);
  };

  return (
    <form onSubmit={handleCreate} className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <label className="block">
          <FieldLabel>Name</FieldLabel>
          <Input
            required
            pattern="[a-zA-Z0-9][a-zA-Z0-9_\-\.]*"
            placeholder="my-channel"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
            Letters, digits, <code>_ - .</code> — first char must be alphanumeric.
          </span>
        </label>
        <label className="block">
          <FieldLabel>Description</FieldLabel>
          <Input
            placeholder="Internal packages"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
      </div>

      <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
        <input
          type="checkbox"
          checked={isPrivate}
          onChange={(e) => setIsPrivate(e.target.checked)}
          className={checkboxCls}
        />
        Private channel (requires auth to browse / download)
      </label>

      <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3.5 dark:border-slate-800 dark:bg-slate-900/40">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={mirrorEnabled}
            onChange={(e) => setMirrorEnabled(e.target.checked)}
            className={checkboxCls}
          />
          Mirror an upstream channel (proxy + cache)
        </label>
        {mirrorEnabled && (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_160px]">
            <label className="block">
              <FieldLabel>Upstream URL</FieldLabel>
              <Input
                required={mirrorEnabled}
                pattern="https?://.+"
                placeholder="https://conda.anaconda.org/conda-forge"
                value={mirrorUrl}
                onChange={(e) => setMirrorUrl(e.target.value)}
              />
              <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                Packages cached on first download; repodata re-fetched after TTL expires.
              </span>
            </label>
            <label className="block">
              <FieldLabel>Repodata TTL (sec)</FieldLabel>
              <Input
                type="number"
                min="0"
                max="86400"
                value={mirrorTtl}
                onChange={(e) => setMirrorTtl(e.target.value)}
              />
            </label>
          </div>
        )}
      </div>

      <div className="flex items-center gap-3">
        <Button type="submit" loading={create.isPending}>
          Create channel
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
      {create.error && <ErrorState error={create.error} />}
    </form>
  );
}
