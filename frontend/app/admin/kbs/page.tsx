"use client";

import { useEffect, useState } from "react";
import { Sparkles, Lock, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { listKbs, deleteKb, type AdminKb } from "@/lib/admin-api";
import { cn } from "@/lib/cn";
import Dialog from "@/components/Dialog";
import AdminShell from "../AdminShell";

const PAGE_SIZE = 50;

/**
 * /admin/kbs — cross-user knowledge base management (06-01).
 *
 * System KBs can't be deleted (backend returns 400); we disable the delete
 * button for them and still surface the error if it somehow fires.
 */
export default function AdminKbsPage() {
  return (
    <AdminShell title="后台管理 · 知识库">
      <KbsTable />
    </AdminShell>
  );
}

function KbsTable() {
  const [kbs, setKbs] = useState<AdminKb[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);

  const [deleteTarget, setDeleteTarget] = useState<AdminKb | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const load = (nextOffset = offset) => {
    setLoading(true);
    listKbs(PAGE_SIZE, nextOffset)
      .then((r) => {
        setKbs(r.kbs);
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

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    try {
      await deleteKb(deleteTarget.id);
      toast.success(`已删除：${deleteTarget.name}`);
      setDeleteTarget(null);
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
      <div className="text-sm text-muted">共 {total} 个知识库</div>

      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted">
              <th className="px-3 py-2 font-medium">名称</th>
              <th className="px-3 py-2 font-medium">所有者</th>
              <th className="px-3 py-2 text-right font-medium">文档</th>
              <th className="px-3 py-2 text-right font-medium">chunks</th>
              <th className="px-3 py-2 text-right font-medium">成员</th>
              <th className="px-3 py-2 font-medium">创建时间</th>
              <th className="px-3 py-2 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {kbs.map((kb) => (
              <tr key={kb.id} className="border-b last:border-0 align-middle">
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    {kb.is_system && <Lock className="h-3.5 w-3.5 text-warning" />}
                    <span className="font-medium">{kb.name}</span>
                    {kb.is_system && (
                      <span className="chip border-warning/30 bg-warning/10 text-warning">
                        系统
                      </span>
                    )}
                  </div>
                  {kb.description && (
                    <div className="truncate text-xs text-muted">{kb.description}</div>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-muted">
                  {kb.owner_email || (
                    <span className="italic opacity-60">系统 / 无主</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {kb.documents_count}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {kb.chunks_count}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {kb.member_count}
                </td>
                <td className="px-3 py-2 text-xs text-muted">
                  {kb.created_at
                    ? new Date(kb.created_at).toLocaleDateString("zh-CN")
                    : "—"}
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center justify-end">
                    <button
                      type="button"
                      title={kb.is_system ? "系统 KB 不可删除" : "删除"}
                      aria-label="删除知识库"
                      disabled={kb.is_system}
                      onClick={() => setDeleteTarget(kb)}
                      className={cn(
                        "rounded-md p-1.5 text-muted/80 transition disabled:opacity-30 disabled:cursor-not-allowed",
                        "hover:bg-danger/15 hover:text-danger"
                      )}
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

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

      <Dialog
        open={deleteTarget != null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
        title={`删除知识库「${deleteTarget?.name ?? ""}」？`}
        description="这个 KB 下所有文档和向量都会一并清除。该操作不可逆。"
        variant="danger"
        confirmLabel="确认删除"
        onConfirm={confirmDelete}
        busy={deleteBusy}
      />
    </div>
  );
}
