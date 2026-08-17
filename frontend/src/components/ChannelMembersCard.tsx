import { useState } from "react";
import { Button } from "./ui/Button";
import { Card, CardBody, CardHeader } from "./ui/Card";
import { ErrorState } from "./ui/EmptyState";
import { Input } from "./ui/Input";
import {
  useAddMember,
  useMembers,
  useRemoveMember,
  useUpdateMember,
} from "@/lib/queries";
import type { Channel, ChannelMember } from "@/lib/types";

const ROLES: ChannelMember["role"][] = ["reader", "writer", "owner"];

/**
 * Per-channel ACL management. Visible only when the caller's ``my_role``
 * on the channel is ``owner`` or ``admin`` — the parent component gates.
 *
 * Intentionally mirrors ChannelAdminCard's layout so owner-grade actions
 * feel like a continuation of the admin surface, not a separate system.
 */
export function ChannelMembersCard({ channel }: { channel: Channel }) {
  const membersQ = useMembers(channel.name);
  const add = useAddMember(channel.name);
  const update = useUpdateMember(channel.name);
  const remove = useRemoveMember(channel.name);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<ChannelMember["role"]>("reader");

  const handleAdd = async () => {
    if (!email.trim()) return;
    await add.mutateAsync({ email: email.trim(), role });
    setEmail("");
  };

  const handleRoleChange = async (
    member: ChannelMember,
    newRole: ChannelMember["role"],
  ) => {
    if (newRole === member.role) return;
    await update.mutateAsync({ userId: member.user_id, role: newRole });
  };

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Members</h2>
        <p className="mt-0.5 text-xs text-slate-600 dark:text-slate-400">
          Owners can add and remove members. Writers can upload and delete
          packages. Readers can only see the channel.
        </p>
      </CardHeader>
      <CardBody className="space-y-4">
        {membersQ.error && <ErrorState error={membersQ.error} />}
        {add.error && <ErrorState error={add.error} />}
        {update.error && <ErrorState error={update.error} />}
        {remove.error && <ErrorState error={remove.error} />}

        {/* Add form */}
        <div className="flex flex-col gap-2 rounded-md border border-slate-200 p-3 sm:flex-row sm:items-center dark:border-slate-800">
          <Input
            type="email"
            placeholder="email@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="flex-1"
          />
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as ChannelMember["role"])}
            className="cursor-pointer rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <Button
            variant="primary"
            size="sm"
            onClick={handleAdd}
            loading={add.isPending}
            disabled={!email.trim()}
          >
            Add
          </Button>
        </div>

        {/* Members list */}
        <div className="divide-y divide-slate-100 rounded-md border border-slate-200 dark:divide-slate-800 dark:border-slate-800">
          {(membersQ.data ?? []).map((m) => (
            <div
              key={m.user_id}
              className="flex flex-col gap-2 px-3 py-2 text-sm sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <div className="truncate text-slate-900 dark:text-slate-100">
                  {m.email ?? <em className="text-slate-400 dark:text-slate-500">no email</em>}
                </div>
                {m.username && (
                  <div className="truncate text-xs text-slate-500 dark:text-slate-400">
                    {m.username}
                  </div>
                )}
              </div>
              <div className="flex items-center gap-2">
                <select
                  value={m.role}
                  onChange={(e) =>
                    handleRoleChange(m, e.target.value as ChannelMember["role"])
                  }
                  disabled={update.isPending}
                  className="cursor-pointer rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:cursor-not-allowed dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => remove.mutateAsync(m.user_id)}
                  loading={remove.isPending && remove.variables === m.user_id}
                >
                  Remove
                </Button>
              </div>
            </div>
          ))}
          {!membersQ.isLoading && (membersQ.data ?? []).length === 0 && (
            <div className="px-3 py-3 text-center text-xs text-slate-500 dark:text-slate-400">
              No members yet — add one above.
            </div>
          )}
        </div>
      </CardBody>
    </Card>
  );
}
