"use client";

import { useEffect, useState } from "react";
import {
  Sparkles,
  ShieldCheck,
  ShieldOff,
  Ban,
  CheckCircle2,
  KeyRound,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import { getUser as getCachedUser } from "@/lib/auth";
import {
  listUsers,
  updateUser,
  resetUserPassword,
  deleteUser,
  type AdminUser,
} from "@/lib/admin-api";
import { cn } from "@/lib/cn";
import Dialog from "@/components/Dialog";
import AdminShell from "../AdminShell";

const PAGE_SIZE = 50;

/**
 * /admin/users — user management table with inline actions (06-01).
 *
 * Actions (ban/unban, grant/revoke admin, reset password, delete) call the
 * admin API; the backend enforces self-protection + last-admin invariants
 * (400 / 409) which we surface verbatim via toast.
 */
export default function AdminUsersPage() {
  return (
    <AdminShell title="后台管理 · 用户">
      <UsersTable />
    </AdminShell>
  );
}

function UsersTable() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);

  // Dialog state
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [resetPwd, setResetPwd] = useState("");
  const [resetBusy, setResetBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const me = getCachedUser();

  const load = (nextOffset = offset) => {
    setLoading(true);
    listUsers(PAGE_SIZE, nextOffset)
      .then((r) => {
        setUsers(r.users);
        setTotal(r.total);
        setOffset(r.offset);
      })
      .catch((e) => toast.error((e as Error).message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const patch = async (u: AdminUser, body: { is_active?: boolean; is_admin?: boolean }) => {
    setBusyId(u.id);
    try {
      const updated = await updateUser(u.id, body);
      setUsers((prev) => prev.map((x) => (x.id === u.id ? updated : x)));
      toast.success("已更新");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  const confirmReset = async () => {
    if (!resetTarget) return;
    if (resetPwd.length < 8) {
      toast.error("新密码至少 8 位");
      return;
    }
    setResetBusy(true);
    try {
      await resetUserPassword(resetTarget.id, resetPwd);
      toast.success(`已为 ${resetTarget.email} 重置密码`);
      setResetTarget(null);
      setResetPwd("");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setResetBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    try {
      await deleteUser(deleteTarget.id);
      toast.success(`已删除：${deleteTarget.email}`);
      setDeleteTarget(null);
      // Reload current page (total shifts).
      load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDeleteBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted">
        <Sparkles className="h-4 w-4 animate-pulse text-accent" />
        加载中…
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="text-sm text-muted">共 {total} 个用户</div>

      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted">
              <th className="px-3 py-2 font-medium">用户</th>
              <th className="px-3 py-2 font-medium">注册时间</th>
              <th className="px-3 py-2 font-medium">状态</th>
              <th className="px-3 py-2 text-right font-medium">KB</th>
              <th className="px-3 py-2 text-right font-medium">会话</th>
              <th className="px-3 py-2 font-medium">LLM</th>
              <th className="px-3 py-2 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const isSelf = me?.id === u.id;
              const busy = busyId === u.id;
              return (
                <tr key={u.id} className="border-b last:border-0 align-middle">
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">
                        {u.display_name?.trim() || u.email}
                      </span>
                      {u.is_admin && (
                        <span className="chip border-accent/30 bg-accent/10 text-accent">
                          管理员
                        </span>
                      )}
                      {isSelf && (
                        <span className="chip border-border bg-surface text-muted">
                          你
                        </span>
                      )}
                    </div>
                    <div className="truncate text-xs text-muted">{u.email}</div>
                  </td>
                  <td className="px-3 py-2 text-xs text-muted">
                    {u.created_at
                      ? new Date(u.created_at).toLocaleDateString("zh-CN")
                      : "—"}
                  </td>
                  <td className="px-3 py-2">
                    {u.is_active ? (
                      <span className="chip border-success/30 bg-success/10 text-success">
                        活跃
                      </span>
                    ) : (
                      <span className="chip border-danger/30 bg-danger/10 text-danger">
                        已封禁
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">{u.kb_count}</td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {u.conversation_count}
                  </td>
                  <td className="px-3 py-2">
                    {u.byok_configured ? (
                      <span className="text-xs text-success">已配置</span>
                    ) : (
                      <span className="text-xs text-muted">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center justify-end gap-1">
                      <IconBtn
                        title={u.is_active ? "封禁" : "解封"}
                        disabled={busy || isSelf}
                        onClick={() => patch(u, { is_active: !u.is_active })}
                        danger={u.is_active}
                      >
                        {u.is_active ? (
                          <Ban className="h-4 w-4" />
                        ) : (
                          <CheckCircle2 className="h-4 w-4" />
                        )}
                      </IconBtn>
                      <IconBtn
                        title={u.is_admin ? "取消管理员" : "设为管理员"}
                        disabled={busy || isSelf}
                        onClick={() => patch(u, { is_admin: !u.is_admin })}
                      >
                        {u.is_admin ? (
                          <ShieldOff className="h-4 w-4" />
                        ) : (
                          <ShieldCheck className="h-4 w-4" />
                        )}
                      </IconBtn>
                      <IconBtn
                        title="重置密码"
                        disabled={busy}
                        onClick={() => {
                          setResetPwd("");
                          setResetTarget(u);
                        }}
                      >
                        <KeyRound className="h-4 w-4" />
                      </IconBtn>
                      <IconBtn
                        title="删除"
                        disabled={busy || isSelf}
                        danger
                        onClick={() => setDeleteTarget(u)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </IconBtn>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {total > PAGE_SIZE && (
        <div className="flex items-center justify-end gap-2 text-sm">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={offset === 0}
            onClick={() => load(Math.max(0, offset - PAGE_SIZE))}
          >
            上一页
          </button>
          <span className="text-xs text-muted">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} / {total}
          </span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => load(offset + PAGE_SIZE)}
          >
            下一页
          </button>
        </div>
      )}

      {/* Reset password dialog */}
      <Dialog
        open={resetTarget != null}
        onOpenChange={(o) => {
          if (!o) {
            setResetTarget(null);
            setResetPwd("");
          }
        }}
        title={`为「${resetTarget?.email ?? ""}」重置密码`}
        description={
          <div className="space-y-2">
            <p>设置一个新密码（至少 8 位）。该用户可立即用新密码登录。</p>
            <input
              type="password"
              value={resetPwd}
              onChange={(e) => setResetPwd(e.target.value)}
              placeholder="新密码"
              className="block w-full rounded-lg border bg-bg px-3 py-2 text-sm text-fg outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
            />
          </div>
        }
        confirmLabel="重置"
        onConfirm={confirmReset}
        busy={resetBusy}
      />

      {/* Delete confirm dialog */}
      <Dialog
        open={deleteTarget != null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
        title={`删除用户「${deleteTarget?.email ?? ""}」？`}
        description="该用户的所有知识库与会话都会一并清除。该操作不可逆。"
        variant="danger"
        confirmLabel="确认删除"
        onConfirm={confirmDelete}
        busy={deleteBusy}
      />
    </div>
  );
}

function IconBtn({
  title,
  onClick,
  disabled,
  danger,
  children,
}: {
  title: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "rounded-md p-1.5 text-muted/80 transition disabled:opacity-30 disabled:cursor-not-allowed",
        danger
          ? "hover:bg-danger/15 hover:text-danger"
          : "hover:bg-accent/15 hover:text-accent"
      )}
    >
      {children}
    </button>
  );
}
