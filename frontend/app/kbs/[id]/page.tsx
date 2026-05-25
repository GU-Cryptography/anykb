"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  ChangeEvent,
  FormEvent,
} from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  Trash2,
  ChevronLeft,
  FileText,
  Link2,
  RefreshCw,
  AlertCircle,
  Lock,
  Hash,
  Layers,
  BookOpen,
  Sparkles,
  Users,
  Eye,
  UserPlus,
  Copy,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { getToken } from "@/lib/auth";
import {
  getKb,
  uploadFile,
  uploadUrl,
  deleteDocument,
  deleteKb,
  patchKb,
  rebuildKb,
  listMembers,
  inviteMember,
  patchMember,
  removeMember,
  listInvitations,
  createInvitation,
  deleteInvitation,
  type KBDetail,
  type Document,
  type DocStatus,
  type KbMemberListResponse,
  type KbInvitation,
  type MemberRole,
  type KbRole,
} from "@/lib/kb-api";
import { toastApiError } from "@/lib/byok-toast";
import { cn } from "@/lib/cn";
import Dialog from "@/components/Dialog";
import Select from "@/components/Select";
import ThemeToggle from "@/components/ThemeToggle";

export default function KbDetailPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const router = useRouter();

  const [kb, setKb] = useState<KBDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);

  const [uploadingFiles, setUploadingFiles] = useState<string[]>([]);
  const [url, setUrl] = useState("");
  const [submittingUrl, setSubmittingUrl] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const [pendingDelete, setPendingDelete] = useState<Document | null>(null);
  const [deleting, setDeleting] = useState(false);

  // v3-M1: owner-only KB-level deletion (danger zone at page bottom)
  const [pendingDeleteKb, setPendingDeleteKb] = useState(false);
  const [deletingKb, setDeletingKb] = useState(false);
  // v3-M3: advanced settings + index rebuild
  const [groupingBusy, setGroupingBusy] = useState(false);
  const [pendingRebuild, setPendingRebuild] = useState(false);
  const [rebuildingKb, setRebuildingKb] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await getKb(id);
      setKb(data);
    } catch (e) {
      toast.error((e as Error).message);
      setNotFound(true);
    }
  }, [id]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    refresh().finally(() => setLoading(false));
  }, [refresh, router]);

  // Poll while any doc is pending/ingesting
  useEffect(() => {
    if (!kb) return;
    const inflight = kb.documents.some(
      (d) => d.status === "pending" || d.status === "ingesting"
    );
    if (!inflight) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [kb, refresh]);

  const onFileChange = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (!files.length) return;
    setUploadingFiles(files.map((f) => f.name));
    try {
      for (const f of files) {
        await uploadFile(id, f);
      }
      toast.success(`已上传 ${files.length} 个文件，正在后台 ingest`);
      await refresh();
    } catch (err) {
      toastApiError(err, (p) => router.push(p));
    } finally {
      setUploadingFiles([]);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const onSubmitUrl = async (e: FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setSubmittingUrl(true);
    try {
      await uploadUrl(id, url.trim());
      toast.success("已提交 URL，正在抓取并 ingest");
      setUrl("");
      await refresh();
    } catch (err) {
      toastApiError(err, (p) => router.push(p));
    } finally {
      setSubmittingUrl(false);
    }
  };

  const confirmDeleteDoc = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await deleteDocument(id, pendingDelete.id);
      toast.success("已删除文档");
      setPendingDelete(null);
      await refresh();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setDeleting(false);
    }
  };

  // v3-M1: KB-level deletion (owner only). Backend cascades members + invitations.
  const confirmDeleteKb = async () => {
    setDeletingKb(true);
    try {
      await deleteKb(id);
      toast.success(`已删除知识库：${kb?.name ?? ""}`);
      router.replace("/kbs");
    } catch (err) {
      toast.error((err as Error).message);
      setDeletingKb(false);
    }
  };

  // v3-M3: toggle grouping_enabled. Optimistic: update UI immediately, revert on error.
  const onToggleGrouping = async (e: ChangeEvent<HTMLInputElement>) => {
    if (!kb) return;
    const next = e.target.checked;
    setGroupingBusy(true);
    // Optimistic local update so the checkbox doesn't lag while PATCH runs.
    setKb({ ...kb, grouping_enabled: next });
    try {
      const updated = await patchKb(id, { grouping_enabled: next });
      // Server is source of truth — splice in only the toggleable field to
      // avoid clobbering documents[] (PATCH response is bare KB without docs).
      setKb((cur) =>
        cur ? { ...cur, grouping_enabled: updated.grouping_enabled } : cur
      );
      toast.success(next ? "已开启 grouping" : "已关闭 grouping");
    } catch (err) {
      setKb((cur) => (cur ? { ...cur, grouping_enabled: !next } : cur));
      toast.error((err as Error).message);
    } finally {
      setGroupingBusy(false);
    }
  };

  // v3-M3: rebuild collection (owner only). Drops + re-ingests every document.
  // After confirm, polling loop in refresh() will surface ingest status.
  const confirmRebuildKb = async () => {
    setRebuildingKb(true);
    try {
      const res = await rebuildKb(id);
      toast.success(`已开始重建：${res.doc_count} 篇文档正在重新 ingest`);
      setPendingRebuild(false);
      // Trigger an immediate refresh so the doc-list polling picks up
      // the pending → ingesting transition without waiting for the timer.
      await refresh();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setRebuildingKb(false);
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-sm text-muted">
        <Sparkles className="h-6 w-6 animate-pulse text-accent" />
        加载中...
      </div>
    );
  }

  if (notFound || !kb) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-sm text-muted">
        <div>找不到这个知识库</div>
        <Link href="/kbs" className="text-accent hover:underline">
          返回列表
        </Link>
      </div>
    );
  }

  // v2-M9: per-KB effective role drives every write button.
  const myRole: KbRole = kb.my_role ?? (kb.is_system ? "viewer" : "owner");
  const isOwner = myRole === "owner";
  const canWrite = (isOwner || myRole === "editor") && !kb.is_system;

  return (
    <div className="min-h-screen bg-bg text-fg">
      <header className="border-b bg-bg/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-4xl items-center gap-3 px-4 sm:px-6">
          <Link
            href="/kbs"
            className="inline-flex items-center gap-1 text-sm text-muted transition hover:text-fg"
          >
            <ChevronLeft className="h-4 w-4" />
            <span>知识库</span>
          </Link>
          <div className="min-w-0 flex-1 truncate text-sm font-medium">{kb.name}</div>
          <button
            onClick={refresh}
            className="rounded-md p-2 transition hover:bg-surface-2"
            title="刷新"
            aria-label="refresh"
            type="button"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-6 sm:px-6">
        {/* v2-M9: role banner for non-owner / non-system access */}
        {!kb.is_system && myRole === "editor" && (
          <div className="card mb-4 border-info/30 bg-info/10 p-3 text-sm">
            <div className="flex items-center gap-2 text-info">
              <Users className="h-4 w-4" />
              <span className="font-medium">你是协作者（editor）</span>
            </div>
            <p className="mt-1 text-xs text-info/90">
              可以上传 / 删除文档；不能删除 KB 或管理成员。
            </p>
          </div>
        )}
        {!kb.is_system && myRole === "viewer" && (
          <div className="card mb-4 border-border bg-surface p-3 text-sm">
            <div className="flex items-center gap-2 text-muted">
              <Eye className="h-4 w-4" />
              <span className="font-medium">你是只读访问者（viewer）</span>
            </div>
            <p className="mt-1 text-xs text-muted">
              可以在对话中选用这个 KB，但不能上传 / 删除内容。
            </p>
          </div>
        )}

        {/* Meta + stats */}
        <div className="card mb-6 p-4">
          <div className="flex items-center gap-2">
            {kb.is_system && <Lock className="h-4 w-4 text-warning" />}
            <span className="text-base font-medium">{kb.name}</span>
            {kb.is_system && (
              <span className="chip border-warning/30 bg-warning/10 text-warning">
                示例 · 只读
              </span>
            )}
          </div>
          {kb.description && (
            <div className="mt-1 text-sm text-muted">{kb.description}</div>
          )}
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat icon={FileText} label="文档" value={kb.documents.length} />
            <Stat icon={Hash} label="chunks" value={kb.chunks_count} />
            <Stat icon={Layers} label="embedding" value={kb.embedding_model || "—"} />
            <Stat icon={BookOpen} label="维度" value={kb.vector_size} />
          </div>
        </div>

        {kb.is_system ? (
          <div className="card mb-6 border-warning/40 bg-warning/10 p-4 text-sm">
            <div className="flex items-center gap-2 font-medium text-warning">
              <Lock className="h-4 w-4" />
              系统内置示例库
            </div>
            <p className="mt-1 text-xs text-warning/90">
              这是 AnyKB 内置的旅行演示知识库（4 城本地餐厅策展数据）。所有用户都能在对话中选中它，体验完整的旅行 Agent 工具链（天气 + POI + 报告生成）。
              本演示库 <strong>只读</strong>：不能上传 / 删除内容。要管理你自己的内容，请回到列表新建一个属于你的 KB。
            </p>
          </div>
        ) : canWrite ? (
          <div className="mb-6 grid gap-3 sm:grid-cols-2">
            <div className="card p-4">
              <div className="mb-2 text-sm font-medium">上传文件</div>
              <input
                ref={fileInput}
                type="file"
                multiple
                accept=".md,.markdown,.txt,.pdf,.docx"
                onChange={onFileChange}
                disabled={uploadingFiles.length > 0}
                className="block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-accent file:px-3 file:py-2 file:text-sm file:text-white hover:file:bg-accent/90 disabled:opacity-50"
              />
              <div className="mt-2 text-xs text-muted">
                支持 .md / .txt / .pdf / .docx（单文件 ≤ 50 MB）
              </div>
              {uploadingFiles.length > 0 && (
                <div className="mt-2 text-xs text-muted">
                  正在上传 {uploadingFiles.join(", ")}…
                </div>
              )}
            </div>

            <form onSubmit={onSubmitUrl} className="card p-4">
              <div className="mb-2 text-sm font-medium">从 URL 抓取</div>
              <input
                type="url"
                required
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://example.com/article"
                className="block w-full rounded-md border bg-bg px-3 py-2 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
              />
              <button
                type="submit"
                disabled={!url.trim() || submittingUrl}
                className="btn btn-primary btn-sm mt-2"
              >
                <Link2 className="h-3 w-3" />
                {submittingUrl ? "提交中…" : "抓取并 ingest"}
              </button>
            </form>
          </div>
        ) : null}

        <div className="card overflow-hidden">
          <div className="border-b px-4 py-2 text-sm font-medium">
            文档（{kb.documents.length}）
            {kb.is_system && (
              <span className="ml-2 text-xs text-muted">
                · 示例库不在 Document 表里展示明细
              </span>
            )}
          </div>
          {kb.documents.length === 0 ? (
            <div className="px-4 py-10 text-center text-sm text-muted">
              {kb.is_system ? (
                <>
                  示例库的策展数据通过 <code className="rounded bg-surface-2 px-1.5 py-0.5">data/ingest.py</code> 灌入 Qdrant，不通过此页面管理。
                  <br />
                  直接在对话中选中它体验即可。
                </>
              ) : (
                <div className="inline-flex flex-col items-center gap-2">
                  <FileText className="h-6 w-6 text-muted/50" />
                  <div>还没有文档</div>
                  <div className="text-xs">上传一份开始 ingest</div>
                </div>
              )}
            </div>
          ) : (
            <ul className="divide-y">
              {kb.documents.map((d) => (
                <DocRow
                  key={d.id}
                  doc={d}
                  readOnly={!canWrite}
                  onDelete={() => setPendingDelete(d)}
                />
              ))}
            </ul>
          )}
        </div>

        {/* v2-M9: members section. Hidden for system KBs (no real owner / no members). */}
        {!kb.is_system && (
          <MembersSection kbId={kb.id} isOwner={isOwner} />
        )}

        {/* v3-M3: owner-only advanced settings — grouping toggle + hybrid rebuild. */}
        {isOwner && !kb.is_system && (
          <section className="card mt-6 p-4">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Sparkles className="h-4 w-4 text-accent" />
              高级设置
            </div>

            <label className="mt-3 flex cursor-pointer items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={kb.grouping_enabled}
                disabled={groupingBusy}
                onChange={onToggleGrouping}
                className="mt-0.5 h-4 w-4 cursor-pointer accent-accent"
              />
              <span>
                <span className="font-medium">Grouping search</span>
                <span className="ml-1 text-xs text-muted">
                  每篇文档至多返回 1 个最相关 chunk，避免长文档独占 top-k。
                </span>
              </span>
            </label>

            <div className="mt-4 border-t border-border pt-3">
              <div className="text-sm font-medium">混合检索索引</div>
              <p className="mt-1 text-xs text-muted">
                启用后会用 BM25 + 向量两路融合检索，关键词查询命中明显改善。
                重建会丢弃当前 chunks 并重新 ingest 所有文档（约 30-90 秒），期间该 KB 临时无召回。
              </p>
              <button
                onClick={() => setPendingRebuild(true)}
                disabled={rebuildingKb}
                className="btn btn-secondary btn-sm mt-2"
                type="button"
              >
                <RefreshCw className={cn("h-4 w-4", rebuildingKb && "animate-spin")} />
                重建索引（启用混合检索）
              </button>
            </div>
          </section>
        )}

        {/* v3-M1: owner-only danger zone for KB deletion. */}
        {isOwner && !kb.is_system && (
          <div className="card mt-6 border-danger/30 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-danger">
              <AlertCircle className="h-4 w-4" />
              危险操作
            </div>
            <p className="mt-1 text-xs text-muted">
              删除知识库会清除所有文档、chunks、成员关系和邀请链接。该操作不可逆。
            </p>
            <button
              onClick={() => setPendingDeleteKb(true)}
              disabled={deletingKb}
              className="btn btn-danger btn-sm mt-3"
              type="button"
            >
              <Trash2 className="h-4 w-4" />
              删除整个知识库
            </button>
          </div>
        )}
      </main>

      <Dialog
        open={pendingDelete != null}
        onOpenChange={(o) => !o && setPendingDelete(null)}
        title={`删除文档「${pendingDelete?.filename ?? ""}」？`}
        description="该文档及其所有 chunks 都会从 Qdrant 中清除。该操作不可逆。"
        variant="danger"
        confirmLabel="确认删除"
        onConfirm={confirmDeleteDoc}
        busy={deleting}
      />

      <Dialog
        open={pendingDeleteKb}
        onOpenChange={(o) => !o && setPendingDeleteKb(false)}
        title={`删除知识库「${kb.name}」？`}
        description="所有文档、chunks、成员关系和邀请链接都会一并清除。该操作不可逆。"
        variant="danger"
        confirmLabel="确认删除整个 KB"
        onConfirm={confirmDeleteKb}
        busy={deletingKb}
      />

      <Dialog
        open={pendingRebuild}
        onOpenChange={(o) => !o && setPendingRebuild(false)}
        title={`重建索引「${kb.name}」？`}
        description="所有文档会被重新 ingest 以启用混合检索 (BM25 + 向量)。约 30-90 秒，期间该 KB 聊天会临时无召回；文档原始文件保留。"
        confirmLabel="确认重建"
        onConfirm={confirmRebuildKb}
        busy={rebuildingKb}
      />
    </div>
  );
}

function Stat({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof FileText;
  label: string;
  value: string | number;
}) {
  return (
    <div className="flex items-start gap-2 rounded-lg bg-surface-2 px-3 py-2">
      <Icon className="mt-0.5 h-3.5 w-3.5 flex-none text-muted" />
      <div className="min-w-0">
        <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
        <div className="truncate text-sm">{value}</div>
      </div>
    </div>
  );
}

function DocRow({
  doc,
  readOnly,
  onDelete,
}: {
  doc: Document;
  readOnly?: boolean;
  onDelete: () => void;
}) {
  return (
    <li className="group flex items-center gap-3 px-4 py-3">
      <FileText className="h-4 w-4 flex-none opacity-60" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm">{doc.filename}</div>
        <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted">
          <StatusBadge status={doc.status} />
          {doc.status === "done" && <span>{doc.chunks_count} chunks</span>}
          {doc.source_type === "file" && doc.size_bytes > 0 && (
            <span>{(doc.size_bytes / 1024).toFixed(1)} KB</span>
          )}
          {doc.source_type === "url" && doc.source_url && (
            <a
              href={doc.source_url}
              target="_blank"
              rel="noreferrer"
              className="max-w-[200px] truncate text-accent hover:underline"
            >
              来源
            </a>
          )}
        </div>
        {doc.status === "failed" && doc.error && (
          <div className="mt-1 flex items-start gap-1 text-xs text-danger">
            <AlertCircle className="mt-0.5 h-3 w-3 flex-none" />
            <span className="truncate">{doc.error}</span>
          </div>
        )}
      </div>
      {!readOnly && (
        <button
          onClick={onDelete}
          className={cn(
            "rounded-md p-1.5 text-muted opacity-0 transition",
            "group-hover:opacity-100",
            "hover:bg-danger/15 hover:text-danger"
          )}
          aria-label="delete document"
          type="button"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      )}
    </li>
  );
}

function StatusBadge({ status }: { status: DocStatus }) {
  const styles: Record<DocStatus, string> = {
    pending: "border-warning/30 bg-warning/10 text-warning",
    ingesting: "border-info/30 bg-info/10 text-info",
    done: "border-success/30 bg-success/10 text-success",
    failed: "border-danger/30 bg-danger/10 text-danger",
  };
  const labels: Record<DocStatus, string> = {
    pending: "排队",
    ingesting: "处理中",
    done: "完成",
    failed: "失败",
  };
  return (
    <span className={cn("chip", styles[status])}>{labels[status]}</span>
  );
}

// ---------------------------------------------------------------------------
// v2-M9: Members section
// ---------------------------------------------------------------------------
function MembersSection({ kbId, isOwner }: { kbId: string; isOwner: boolean }) {
  const [data, setData] = useState<KbMemberListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [pendingRemove, setPendingRemove] = useState<{
    user_id: string;
    email: string;
  } | null>(null);
  const [removing, setRemoving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await listMembers(kbId);
      setData(d);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onChangeRole = async (userId: string, role: MemberRole) => {
    try {
      await patchMember(kbId, userId, role);
      toast.success("已更新角色");
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const confirmRemove = async () => {
    if (!pendingRemove) return;
    setRemoving(true);
    try {
      await removeMember(kbId, pendingRemove.user_id);
      toast.success(`已移除 ${pendingRemove.email}`);
      setPendingRemove(null);
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setRemoving(false);
    }
  };

  return (
    <div className="card mt-6 overflow-hidden">
      <div className="flex items-center justify-between border-b px-4 py-2">
        <div className="text-sm font-medium">
          成员（{(data?.members?.length ?? 0) + (data?.owner ? 1 : 0)}）
        </div>
        {isOwner && (
          <button
            onClick={() => setInviteOpen(true)}
            className="btn btn-primary btn-sm"
            type="button"
          >
            <UserPlus className="h-3 w-3" />
            邀请
          </button>
        )}
      </div>
      {loading ? (
        <div className="px-4 py-6 text-center text-sm text-muted">加载中…</div>
      ) : (
        <ul className="divide-y">
          {data?.owner && (
            <li className="flex items-center gap-3 px-4 py-3">
              <BookOpen className="h-4 w-4 flex-none text-accent" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm">{data.owner.email}</div>
                <div className="text-xs text-muted">
                  {data.owner.display_name || "—"}
                </div>
              </div>
              <span className="chip border-accent/30 bg-accent/10 text-accent">
                owner
              </span>
            </li>
          )}
          {data?.members.map((m) => (
            <li key={m.user_id} className="flex items-center gap-3 px-4 py-3">
              {m.role === "editor" ? (
                <Users className="h-4 w-4 flex-none text-info" />
              ) : (
                <Eye className="h-4 w-4 flex-none text-muted" />
              )}
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm">{m.email}</div>
                <div className="text-xs text-muted">
                  {m.display_name || "—"}
                  {m.invited_by_email && (
                    <> · 由 {m.invited_by_email} 邀请</>
                  )}
                </div>
              </div>
              {isOwner ? (
                <>
                  <Select
                    size="sm"
                    value={m.role}
                    onChange={(e) =>
                      onChangeRole(m.user_id, e.target.value as MemberRole)
                    }
                    options={[
                      { value: "editor", label: "editor" },
                      { value: "viewer", label: "viewer" },
                    ]}
                    className="w-[100px]"
                  />
                  <button
                    onClick={() =>
                      setPendingRemove({ user_id: m.user_id, email: m.email })
                    }
                    className="rounded-md p-1.5 text-muted transition hover:bg-danger/15 hover:text-danger"
                    aria-label="remove member"
                    type="button"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </>
              ) : (
                <span
                  className={cn(
                    "chip",
                    m.role === "editor"
                      ? "border-info/30 bg-info/10 text-info"
                      : "border-border bg-surface text-muted"
                  )}
                >
                  {m.role}
                </span>
              )}
            </li>
          ))}
          {data?.members.length === 0 && !data?.owner && (
            <li className="px-4 py-6 text-center text-sm text-muted">
              暂无成员
            </li>
          )}
        </ul>
      )}

      {isOwner && (
        <InviteDialog
          kbId={kbId}
          open={inviteOpen}
          onClose={() => setInviteOpen(false)}
          onInvited={refresh}
        />
      )}

      <Dialog
        open={pendingRemove != null}
        onOpenChange={(o) => !o && setPendingRemove(null)}
        title={`移除 ${pendingRemove?.email ?? ""}？`}
        description="该用户将失去对此 KB 的访问。可重新邀请。"
        variant="danger"
        confirmLabel="确认移除"
        onConfirm={confirmRemove}
        busy={removing}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// v2-M9: Invite dialog — two tabs (by email, by link)
// ---------------------------------------------------------------------------
function InviteDialog({
  kbId,
  open,
  onClose,
  onInvited,
}: {
  kbId: string;
  open: boolean;
  onClose: () => void;
  onInvited: () => void;
}) {
  const [tab, setTab] = useState<"email" | "link">("email");
  const [email, setEmail] = useState("");
  const [emailRole, setEmailRole] = useState<MemberRole>("editor");
  const [emailBusy, setEmailBusy] = useState(false);

  const [linkRole, setLinkRole] = useState<MemberRole>("viewer");
  const [linkExpiresHours, setLinkExpiresHours] = useState<string>("");
  const [linkMaxUses, setLinkMaxUses] = useState<string>("");
  const [linkBusy, setLinkBusy] = useState(false);
  const [invitations, setInvitations] = useState<KbInvitation[]>([]);

  const reload = useCallback(async () => {
    try {
      const list = await listInvitations(kbId);
      setInvitations(list);
    } catch (e) {
      console.warn("listInvitations failed (non-fatal)", e);
    }
  }, [kbId]);

  useEffect(() => {
    if (open) {
      setTab("email");
      setEmail("");
      setEmailRole("editor");
      setLinkRole("viewer");
      setLinkExpiresHours("");
      setLinkMaxUses("");
      void reload();
    }
  }, [open, reload]);

  const onInviteEmail = async (e: FormEvent) => {
    e.preventDefault();
    if (!email.trim()) return;
    setEmailBusy(true);
    try {
      await inviteMember(kbId, email.trim().toLowerCase(), emailRole);
      toast.success(`已邀请 ${email.trim()} 为 ${emailRole}`);
      setEmail("");
      onInvited();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setEmailBusy(false);
    }
  };

  const onCreateLink = async () => {
    setLinkBusy(true);
    try {
      const hours = linkExpiresHours.trim() ? Number(linkExpiresHours) : null;
      const maxUses = linkMaxUses.trim() ? Number(linkMaxUses) : null;
      const expires_at =
        hours && hours > 0
          ? new Date(Date.now() + hours * 3600 * 1000).toISOString()
          : null;
      await createInvitation(kbId, {
        role: linkRole,
        expires_at,
        max_uses: maxUses,
      });
      toast.success("已生成分享链接");
      await reload();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setLinkBusy(false);
    }
  };

  const onRevoke = async (invId: string) => {
    try {
      await deleteInvitation(kbId, invId);
      toast.success("已撤销链接");
      await reload();
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  const buildUrl = (token: string) => {
    if (typeof window === "undefined") return `/invite/${token}`;
    return `${window.location.origin}/invite/${token}`;
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("已复制");
    } catch {
      toast.error("复制失败");
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        className="card w-full max-w-lg overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div className="text-sm font-medium">邀请协作者</div>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-muted hover:bg-surface-2"
            aria-label="close"
            type="button"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex border-b">
          <button
            type="button"
            onClick={() => setTab("email")}
            className={cn(
              "flex-1 px-4 py-2 text-sm transition",
              tab === "email"
                ? "border-b-2 border-accent text-fg"
                : "text-muted hover:text-fg"
            )}
          >
            按邮箱邀请
          </button>
          <button
            type="button"
            onClick={() => setTab("link")}
            className={cn(
              "flex-1 px-4 py-2 text-sm transition",
              tab === "link"
                ? "border-b-2 border-accent text-fg"
                : "text-muted hover:text-fg"
            )}
          >
            生成分享链接
          </button>
        </div>

        <div className="p-4">
          {tab === "email" ? (
            <form onSubmit={onInviteEmail} className="space-y-3">
              <div className="text-xs text-muted">
                被邀请者必须先在 AnyKB 注册一个账号，再用该邮箱邀请。
              </div>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="bob@example.com"
                className="block w-full rounded-md border bg-bg px-3 py-2 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
              />
              <div className="flex items-center gap-2">
                <label className="text-xs text-muted">角色</label>
                <Select
                  size="sm"
                  value={emailRole}
                  onChange={(e) => setEmailRole(e.target.value as MemberRole)}
                  options={[
                    { value: "editor", label: "editor（读+写文档）" },
                    { value: "viewer", label: "viewer（只读）" },
                  ]}
                  className="flex-1"
                />
              </div>
              <button
                type="submit"
                disabled={emailBusy || !email.trim()}
                className="btn btn-primary w-full"
              >
                {emailBusy ? "邀请中…" : "发送邀请"}
              </button>
            </form>
          ) : (
            <div className="space-y-4">
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <label className="text-xs text-muted w-16">角色</label>
                  <Select
                    size="sm"
                    value={linkRole}
                    onChange={(e) => setLinkRole(e.target.value as MemberRole)}
                    options={[
                      { value: "viewer", label: "viewer（只读）" },
                      { value: "editor", label: "editor（读+写）" },
                    ]}
                    className="flex-1"
                  />
                </div>
                <div className="flex items-center gap-2">
                  <label className="text-xs text-muted w-16">有效期</label>
                  <input
                    type="number"
                    min="0"
                    value={linkExpiresHours}
                    onChange={(e) => setLinkExpiresHours(e.target.value)}
                    placeholder="留空 = 永不过期"
                    className="flex-1 rounded-md border bg-bg px-3 py-1.5 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
                  />
                  <span className="text-xs text-muted">小时</span>
                </div>
                <div className="flex items-center gap-2">
                  <label className="text-xs text-muted w-16">最大次数</label>
                  <input
                    type="number"
                    min="1"
                    value={linkMaxUses}
                    onChange={(e) => setLinkMaxUses(e.target.value)}
                    placeholder="留空 = 不限"
                    className="flex-1 rounded-md border bg-bg px-3 py-1.5 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
                  />
                  <span className="text-xs text-muted">次</span>
                </div>
                <button
                  onClick={onCreateLink}
                  disabled={linkBusy}
                  className="btn btn-primary w-full"
                  type="button"
                >
                  {linkBusy ? "生成中…" : "生成新链接"}
                </button>
              </div>

              {invitations.length > 0 && (
                <div className="mt-4 border-t pt-3">
                  <div className="mb-2 text-xs font-medium text-muted">
                    现有链接
                  </div>
                  <ul className="space-y-2">
                    {invitations.map((inv) => (
                      <li
                        key={inv.id}
                        className={cn(
                          "rounded-md border bg-surface-2 p-2 text-xs",
                          inv.revoked && "opacity-50"
                        )}
                      >
                        <div className="flex items-center gap-1.5">
                          <span className="chip border-border bg-surface text-muted">
                            {inv.role}
                          </span>
                          {inv.max_uses != null && (
                            <span className="text-muted">
                              {inv.uses_count}/{inv.max_uses} 次
                            </span>
                          )}
                          {inv.expires_at && (
                            <span className="text-muted">
                              到期 {new Date(inv.expires_at).toLocaleString()}
                            </span>
                          )}
                          {inv.revoked && (
                            <span className="text-danger">已撤销</span>
                          )}
                          <div className="flex-1" />
                          {!inv.revoked && (
                            <>
                              <button
                                onClick={() => copy(buildUrl(inv.id))}
                                className="rounded p-1 hover:bg-accent/15 hover:text-accent"
                                title="复制链接"
                                type="button"
                              >
                                <Copy className="h-3 w-3" />
                              </button>
                              <button
                                onClick={() => onRevoke(inv.id)}
                                className="rounded p-1 hover:bg-danger/15 hover:text-danger"
                                title="撤销"
                                type="button"
                              >
                                <X className="h-3 w-3" />
                              </button>
                            </>
                          )}
                        </div>
                        {!inv.revoked && (
                          <div className="mt-1 break-all font-mono text-[10px] text-muted">
                            {buildUrl(inv.id)}
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
