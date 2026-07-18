"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Brain, ChevronLeft, Sparkles, Trash2, Pencil } from "lucide-react";
import { toast } from "sonner";

import { getToken } from "@/lib/auth";
import {
  listMemories,
  updateMemory,
  deleteMemory,
  type Memory,
  type MemoryStats,
  type MemoryType,
} from "@/lib/memories-api";
import { cn } from "@/lib/cn";
import Dialog from "@/components/Dialog";
import ThemeToggle from "@/components/ThemeToggle";

const PAGE_SIZE = 50;

/**
 * /memories — long-term memory management page (v3-M4, PRD §8).
 *
 * Lists the current user's extracted memories with a type filter + pagination,
 * inline delete (confirm) and edit (content + importance). Mirrors the /kbs
 * page chrome (auth guard → /login, back-to-chat header, ThemeToggle) and the
 * admin table's pagination. The backend owner-scopes every row.
 */

// Type → display label + chip color (mirrors the KB/admin chip palette).
const TYPE_META: Record<MemoryType, { label: string; chip: string }> = {
  profile: { label: "画像", chip: "border-accent/30 bg-accent/10 text-accent" },
  preference: { label: "偏好", chip: "border-info/30 bg-info/10 text-info" },
  fact: { label: "事实", chip: "border-success/30 bg-success/10 text-success" },
  task: { label: "任务", chip: "border-warning/30 bg-warning/10 text-warning" },
  skill: { label: "技能", chip: "border-accent/30 bg-accent/10 text-accent" },
  explicit: { label: "显式", chip: "border-danger/30 bg-danger/10 text-danger" },
};

const FILTERS: { value: MemoryType | null; label: string }[] = [
  { value: null, label: "全部" },
  { value: "profile", label: "画像" },
  { value: "preference", label: "偏好" },
  { value: "fact", label: "事实" },
  { value: "task", label: "任务" },
  { value: "skill", label: "技能" },
  { value: "explicit", label: "显式" },
];

export default function MemoriesPage() {
  const router = useRouter();
  const [memories, setMemories] = useState<Memory[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<MemoryType | null>(null);
  const [loading, setLoading] = useState(true);

  // Edit dialog state
  const [editTarget, setEditTarget] = useState<Memory | null>(null);
  const [editContent, setEditContent] = useState("");
  const [editImportance, setEditImportance] = useState(0.5);
  const [editBusy, setEditBusy] = useState(false);

  // Delete dialog state
  const [deleteTarget, setDeleteTarget] = useState<Memory | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const load = (nextOffset = offset, nextFilter = filter) => {
    setLoading(true);
    listMemories({ type: nextFilter, limit: PAGE_SIZE, offset: nextOffset })
      .then((r) => {
        setMemories(r.memories);
        setTotal(r.total);
        setStats(r.stats);
        setOffset(r.offset);
      })
      .catch((e) => toast.error((e as Error).message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    load(0, null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  const applyFilter = (next: MemoryType | null) => {
    setFilter(next);
    setOffset(0);
    load(0, next);
  };

  const openEdit = (m: Memory) => {
    setEditTarget(m);
    setEditContent(m.content);
    setEditImportance(m.importance);
  };

  const confirmEdit = async () => {
    if (!editTarget) return;
    const content = editContent.trim();
    if (!content) {
      toast.error("内容不能为空");
      return;
    }
    setEditBusy(true);
    try {
      const updated = await updateMemory(editTarget.id, {
        content,
        importance: editImportance,
      });
      setMemories((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
      toast.success("已更新");
      setEditTarget(null);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setEditBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    try {
      await deleteMemory(deleteTarget.id);
      toast.success("已删除");
      setDeleteTarget(null);
      load(); // total shifts — reload current page
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-bg text-fg">
      <header className="border-b bg-bg/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-4xl items-center gap-3 px-4 sm:px-6">
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-sm text-muted transition hover:text-fg"
          >
            <ChevronLeft className="h-4 w-4" />
            <span>返回对话</span>
          </Link>
          <div className="flex-1" />
          <h1 className="flex items-center gap-2 text-sm font-medium">
            <Brain className="h-4 w-4" />
            我的记忆
          </h1>
          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-6 sm:px-6">
        <div className="mb-4 text-sm text-muted">
          这里是助手从你的对话中长期记住的信息，会在后续对话中作为背景注入。你可以编辑或删除任意一条。
        </div>

        {/* Stats bar (v3-M5): active total + per-type count badges. */}
        {stats && stats.active_total > 0 && (
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted">
              共 {stats.active_total} 条长期记忆
            </span>
            {FILTERS.filter((f) => f.value != null).map((f) => {
              const count = stats.by_type[f.value as MemoryType] ?? 0;
              if (count === 0) return null;
              const meta = TYPE_META[f.value as MemoryType];
              return (
                <span key={f.label} className={cn("chip", meta.chip)}>
                  {meta.label} {count}
                </span>
              );
            })}
          </div>
        )}

        {/* Type filter */}
        <div className="mb-4 flex flex-wrap gap-1.5">
          {FILTERS.map((f) => {
            const active = filter === f.value;
            return (
              <button
                key={f.label}
                type="button"
                onClick={() => applyFilter(f.value)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm transition",
                  active
                    ? "bg-accent/15 text-fg"
                    : "text-muted hover:bg-surface hover:text-fg"
                )}
              >
                {f.label}
              </button>
            );
          })}
        </div>

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted">
            <Sparkles className="h-4 w-4 animate-pulse text-accent" />
            加载中…
          </div>
        ) : memories.length === 0 ? (
          <div className="card flex flex-col items-center gap-3 border-dashed py-12 text-center">
            <Brain className="h-6 w-6 text-accent" />
            <div className="text-sm">还没有长期记忆</div>
            <div className="text-xs text-muted">
              多聊几句，或在会话结束时点「结束会话并提取记忆」
            </div>
          </div>
        ) : (
          <>
            <div className="mb-3 text-sm text-muted">共 {total} 条记忆</div>
            <ul className="space-y-2">
              {memories.map((m) => {
                const meta = TYPE_META[m.type] ?? {
                  label: m.type,
                  chip: "border-border bg-surface text-muted",
                };
                return (
                  <li key={m.id} className="card group px-4 py-3">
                    <div className="flex items-start gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className={cn("chip", meta.chip)}>{meta.label}</span>
                          <span className="text-xs text-muted">
                            重要度 {m.importance.toFixed(2)}
                          </span>
                          {m.created_at && (
                            <span className="text-xs text-muted">
                              {new Date(m.created_at).toLocaleDateString("zh-CN")}
                            </span>
                          )}
                        </div>
                        <div className="mt-1.5 whitespace-pre-wrap break-words text-sm">
                          {m.content}
                        </div>
                      </div>
                      <div className="flex flex-none items-center gap-1">
                        <IconBtn title="编辑" onClick={() => openEdit(m)}>
                          <Pencil className="h-4 w-4" />
                        </IconBtn>
                        <IconBtn title="删除" danger onClick={() => setDeleteTarget(m)}>
                          <Trash2 className="h-4 w-4" />
                        </IconBtn>
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>

            {/* Pagination */}
            {total > PAGE_SIZE && (
              <div className="mt-4 flex items-center justify-end gap-2 text-sm">
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
          </>
        )}
      </main>

      {/* Edit dialog — content + importance */}
      <Dialog
        open={editTarget != null}
        onOpenChange={(o) => !o && setEditTarget(null)}
        title="编辑记忆"
        description={
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-xs text-muted">内容</label>
              <textarea
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                rows={3}
                maxLength={4096}
                className="block w-full resize-y rounded-lg border bg-bg px-3 py-2 text-sm text-fg outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
              />
            </div>
            <div>
              <label className="mb-1 flex items-center justify-between text-xs text-muted">
                <span>重要度</span>
                <span className="tabular-nums">{editImportance.toFixed(2)}</span>
              </label>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={editImportance}
                onChange={(e) => setEditImportance(Number(e.target.value))}
                className="w-full accent-accent"
              />
            </div>
          </div>
        }
        confirmLabel="保存"
        onConfirm={confirmEdit}
        busy={editBusy}
      />

      {/* Delete confirm dialog */}
      <Dialog
        open={deleteTarget != null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
        title="删除这条记忆？"
        description="删除后，助手将不再在后续对话中使用这条记忆。该操作不可逆。"
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
  danger,
  children,
}: {
  title: string;
  onClick: () => void;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      className={cn(
        "rounded-md p-1.5 text-muted/80 transition",
        danger
          ? "hover:bg-danger/15 hover:text-danger"
          : "hover:bg-accent/15 hover:text-accent"
      )}
    >
      {children}
    </button>
  );
}
