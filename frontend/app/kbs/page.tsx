"use client";

import { useEffect, useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  Plus,
  Trash2,
  BookOpen,
  ChevronLeft,
  Lock,
  Sparkles,
  FileText,
  Hash,
  Users,
  Eye,
  X,
  ChevronDown,
  ChevronRight,
  KeyRound,
} from "lucide-react";
import { toast } from "sonner";

import { getToken } from "@/lib/auth";
import { listKbs, createKb, deleteKb, type KB, type CreateKbBody } from "@/lib/kb-api";
import {
  getMySettings,
  probeEmbedding,
  probeReranker,
  saveEmbeddingSettings,
  saveRerankerSettings,
  type MySettings,
  type EmbeddingProvider,
  type RerankerProvider,
} from "@/lib/settings-api";
import { toastApiError } from "@/lib/byok-toast";
import { cn } from "@/lib/cn";
import Dialog from "@/components/Dialog";
import ThemeToggle from "@/components/ThemeToggle";

export default function KbsPage() {
  const router = useRouter();
  const [kbs, setKbs] = useState<KB[]>([]);
  const [loading, setLoading] = useState(true);

  const [createOpen, setCreateOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<KB | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    listKbs()
      .then(setKbs)
      .catch((e) => toast.error((e as Error).message))
      .finally(() => setLoading(false));
  }, [router]);

  const onCreated = (kb: KB) => {
    setKbs((prev) => [kb, ...prev]);
    setCreateOpen(false);
    toast.success(`已创建：${kb.name}`);
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await deleteKb(pendingDelete.id);
      setKbs((prev) => prev.filter((k) => k.id !== pendingDelete.id));
      toast.success(`已删除：${pendingDelete.name}`);
      setPendingDelete(null);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDeleting(false);
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
            <BookOpen className="h-4 w-4" />
            我的知识库
          </h1>
          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-6 sm:px-6">
        <div className="mb-6 flex items-center justify-between">
          <div className="text-sm text-muted">
            创建新知识库时可选择此 KB 使用的 embedding / reranker 配置。
          </div>
          <button
            onClick={() => setCreateOpen(true)}
            className="btn btn-primary"
            type="button"
          >
            <Plus className="h-4 w-4" />
            新建知识库
          </button>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted">
            <Sparkles className="h-4 w-4 animate-pulse text-accent" />
            加载中...
          </div>
        ) : kbs.length === 0 ? (
          <div className="card flex flex-col items-center gap-3 border-dashed py-12 text-center">
            <Sparkles className="h-6 w-6 text-accent" />
            <div className="text-sm">还没有知识库</div>
            <div className="text-xs text-muted">点上面「新建知识库」开始</div>
          </div>
        ) : (
          <ul className="space-y-2">
            {kbs.map((kb) => {
              const isOwner = kb.my_role === "owner";
              const isEditor = kb.my_role === "editor";
              const isViewer = kb.my_role === "viewer" && !kb.is_system;
              return (
                <li key={kb.id} className="card card-hover group">
                  <div className="flex items-center gap-3 px-4 py-3">
                    <Link href={`/kbs/${kb.id}`} className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        {kb.is_system ? (
                          <Lock className="h-4 w-4 text-warning" />
                        ) : isEditor ? (
                          <Users className="h-4 w-4 text-info" />
                        ) : isViewer ? (
                          <Eye className="h-4 w-4 text-muted" />
                        ) : (
                          <BookOpen className="h-4 w-4 opacity-60" />
                        )}
                        <span className="truncate font-medium">{kb.name}</span>
                        {kb.is_system && (
                          <span className="chip border-warning/30 bg-warning/10 text-warning">
                            示例 · 只读
                          </span>
                        )}
                        {isEditor && (
                          <span className="chip border-info/30 bg-info/10 text-info">
                            协作
                          </span>
                        )}
                        {isViewer && (
                          <span className="chip border-border bg-surface text-muted">
                            只读
                          </span>
                        )}
                      </div>
                      <div className="mt-1 truncate text-xs text-muted">
                        {kb.description || (
                          <span className="italic opacity-60">无描述</span>
                        )}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                        <span className="inline-flex items-center gap-1">
                          <FileText className="h-3 w-3" />
                          {kb.documents_count} 文档
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <Hash className="h-3 w-3" />
                          {kb.chunks_count} chunks
                        </span>
                        <span className="truncate">
                          {kb.embedding_model || "—"}
                        </span>
                      </div>
                    </Link>
                    {isOwner && (
                      <button
                        onClick={() => setPendingDelete(kb)}
                        className={cn(
                          "rounded-md p-2 text-muted/70 transition",
                          "hover:bg-danger/15 hover:text-danger"
                        )}
                        aria-label="delete kb"
                        title="删除知识库"
                        type="button"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </main>

      <CreateKbDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={onCreated}
        onByokRedirect={(p) => router.push(p)}
      />

      <Dialog
        open={pendingDelete != null}
        onOpenChange={(o) => !o && setPendingDelete(null)}
        title={`删除知识库「${pendingDelete?.name ?? ""}」？`}
        description="这个 KB 下所有文档和向量都会一并清除。该操作不可逆。"
        variant="danger"
        confirmLabel="确认删除"
        onConfirm={confirmDelete}
        busy={deleting}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// v3-M7: KB creation dialog with optional per-KB embedding + reranker config
// ---------------------------------------------------------------------------
function CreateKbDialog({
  open,
  onClose,
  onCreated,
  onByokRedirect,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (kb: KB) => void;
  onByokRedirect: (path: string) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);

  // v3-M8: embedding section — no more "inherit/custom" toggle. Form is the
  // single source of truth; on open we prefill from user-saved cfg (provider /
  // base_url / model) and leave api_key blank with a "已保存" placeholder
  // when the user has previously saved a key. The backend transparently
  // reuses the saved decrypted key when api_key arrives empty.
  const [embedProvider, setEmbedProvider] =
    useState<EmbeddingProvider>("openai-compat");
  const [embedBaseUrl, setEmbedBaseUrl] = useState(
    "https://api.siliconflow.cn/v1"
  );
  const [embedApiKey, setEmbedApiKey] = useState("");
  const [embedModel, setEmbedModel] = useState("BAAI/bge-m3");
  const [embedDim, setEmbedDim] = useState<number | null>(null);
  const [embedProbing, setEmbedProbing] = useState(false);
  const [embedKeySaved, setEmbedKeySaved] = useState(false);
  // v3-M8.1: probe → models list → Select dropdown (mirror /settings LLMCard).
  const [embedModels, setEmbedModels] = useState<string[]>([]);
  // v3-M8.2: must successfully test the connection before "创建" enables.
  // Prefilled cfg from user-level settings counts as already-verified (the
  // user must have probed before saving it to /settings). Any edit to
  // base_url / api_key / model resets this flag, forcing a re-test. Backend
  // also probes as a second line of defense (502 on connection failure).
  const [embedVerified, setEmbedVerified] = useState(false);
  // v3-M8.3: when user has a saved api_key (has_key=true), default to a
  // compact "已使用保存的密钥（修改）" chip instead of the empty password
  // input — the "已保存（留空保持现有）" placeholder confused users into
  // thinking they had to retype the key. Toggled to input mode when they
  // explicitly click "修改" or when cfg fields drift away from the user's
  // saved cfg (so a new provider/base_url forces re-entry).
  const [embedKeyEditing, setEmbedKeyEditing] = useState(false);

  // Reranker section: "off" (default) | "custom". Same prefill behavior.
  const [rerankerMode, setRerankerMode] = useState<"off" | "custom">("off");
  const [rerankerExpanded, setRerankerExpanded] = useState(false);
  const [rerankerProvider, setRerankerProvider] =
    useState<RerankerProvider>("siliconflow");
  const [rerankerBaseUrl, setRerankerBaseUrl] = useState(
    "https://api.siliconflow.cn/v1"
  );
  const [rerankerApiKey, setRerankerApiKey] = useState("");
  const [rerankerModel, setRerankerModel] = useState(
    "BAAI/bge-reranker-v2-m3"
  );
  const [rerankerProbing, setRerankerProbing] = useState(false);
  const [rerankerKeySaved, setRerankerKeySaved] = useState(false);
  const [rerankerModels, setRerankerModels] = useState<string[]>([]);
  const [rerankerVerified, setRerankerVerified] = useState(false);
  const [rerankerKeyEditing, setRerankerKeyEditing] = useState(false);

  useEffect(() => {
    if (!open) return;
    // Reset form on open
    setName("");
    setDescription("");
    setRerankerMode("off");
    setRerankerExpanded(false);
    setEmbedApiKey("");
    setRerankerApiKey("");
    setEmbedDim(null);
    setEmbedModels([]);
    setRerankerModels([]);
    setEmbedVerified(false);
    setRerankerVerified(false);
    setEmbedKeyEditing(false);
    setRerankerKeyEditing(false);
    // v3-M8: prefill embedding/reranker form from user's saved cfg ("暗中记忆").
    // Provider + base_url + model carry over; api_key stays blank with a
    // "已保存（留空保持现有）" placeholder when has_key is true.
    getMySettings()
      .then((s) => {
        if (s.embedding.provider) {
          setEmbedProvider(s.embedding.provider as EmbeddingProvider);
        }
        if (s.embedding.base_url) setEmbedBaseUrl(s.embedding.base_url);
        if (s.embedding.model) setEmbedModel(s.embedding.model);
        if (s.embedding.dim) setEmbedDim(s.embedding.dim);
        setEmbedKeySaved(Boolean(s.embedding.has_key));
        // v3-M8.2: if user has a complete saved embedding cfg, treat the
        // prefilled form as already-verified — they must have probed before
        // saving it to /settings. Changing any field will reset this.
        if (
          s.embedding.has_key &&
          s.embedding.provider &&
          s.embedding.base_url &&
          s.embedding.model &&
          s.embedding.dim
        ) {
          setEmbedVerified(true);
        }

        if (s.reranker.provider) {
          setRerankerProvider(s.reranker.provider as RerankerProvider);
        }
        if (s.reranker.base_url) setRerankerBaseUrl(s.reranker.base_url);
        if (s.reranker.model) setRerankerModel(s.reranker.model);
        setRerankerKeySaved(Boolean(s.reranker.has_key));
        if (
          s.reranker.has_key &&
          s.reranker.provider &&
          s.reranker.base_url &&
          s.reranker.model
        ) {
          setRerankerVerified(true);
        }
      })
      .catch(() => {
        /* user has no saved cfg yet — defaults stay as-is */
      });
  }, [open]);

  // v3-M8.1: "测试连接" — probe provider; on success populate models dropdown
  // AND probe current model's dim. On failure surface the upstream error so
  // the user knows their api_key is wrong BEFORE ingest tries to use it.
  const onTestEmbedding = async () => {
    if (!embedBaseUrl) {
      toast.error("请填写 base_url");
      return;
    }
    setEmbedProbing(true);
    try {
      const r = await probeEmbedding({
        provider: embedProvider,
        base_url: embedBaseUrl,
        api_key: embedApiKey,  // backend falls back to saved key when empty
        model: embedModel || undefined,
      });
      setEmbedModels(r.models);
      if (r.dim) setEmbedDim(r.dim);
      setEmbedVerified(true);
      const dimMsg = r.dim ? ` · 当前模型 ${r.dim} 维` : "";
      toast.success(`连接成功：${r.models.length} 个模型${dimMsg}`);
    } catch (e) {
      setEmbedVerified(false);
      toast.error(e instanceof Error ? e.message : "连接失败");
    } finally {
      setEmbedProbing(false);
    }
  };

  const onTestReranker = async () => {
    if (!rerankerBaseUrl) {
      toast.error("请填写 base_url");
      return;
    }
    setRerankerProbing(true);
    try {
      const r = await probeReranker({
        provider: rerankerProvider,
        base_url: rerankerBaseUrl,
        api_key: rerankerApiKey,
      });
      setRerankerModels(r.models);
      setRerankerVerified(true);
      toast.success(`连接成功：${r.models.length} 个模型`);
    } catch (e) {
      setRerankerVerified(false);
      toast.error(e instanceof Error ? e.message : "连接失败");
    } finally {
      setRerankerProbing(false);
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    const body: CreateKbBody = {
      name: name.trim(),
      description: description.trim(),
    };

    // v3-M8: embedding is always required (per-KB). Validate before submit.
    if (!embedProvider || !embedBaseUrl || !embedModel) {
      toast.error("请补全 Embedding 配置");
      return;
    }
    if (embedDim == null) {
      toast.error("请先点「测试连接」探测向量维度");
      return;
    }
    // v3-M8.2: must have successfully tested the connection. Without this,
    // a wrong/empty api_key creates a "valid-looking" KB that 403s on first
    // upload — confusing and hard to recover from.
    if (!embedVerified) {
      toast.error("请先点「测试连接 / 拉取模型」验证 Embedding 可用");
      return;
    }
    body.embedding_provider = embedProvider;
    body.embedding_base_url = embedBaseUrl;
    body.embedding_api_key = embedApiKey;  // backend fills in saved key if empty
    body.embedding_model = embedModel;
    body.embedding_dim = embedDim;

    if (rerankerMode === "custom") {
      if (!rerankerProvider || !rerankerBaseUrl || !rerankerModel) {
        toast.error("请补全 Reranker 配置");
        return;
      }
      if (!rerankerVerified) {
        toast.error("请先点「测试连接 / 拉取模型」验证 Reranker 可用");
        return;
      }
      body.reranker_provider = rerankerProvider;
      body.reranker_base_url = rerankerBaseUrl;
      body.reranker_api_key = rerankerApiKey;
      body.reranker_model = rerankerModel;
      body.reranker_enabled = true;
    }

    setCreating(true);
    try {
      const kb = await createKb(body);

      // v3-M8: "暗中记忆" — fire-and-forget save the embedding/reranker cfg
      // to the user record so the next KB creation form prefills from it.
      // Silent on failure (cfg sync is a convenience, not a critical path).
      try {
        await saveEmbeddingSettings({
          provider: embedProvider,
          base_url: embedBaseUrl,
          api_key: embedApiKey,  // empty = keep existing (backend semantics)
          model: embedModel,
          dim: embedDim,
        });
      } catch {
        /* ignore — likely a dim conflict 409 against other un-cfg'd KBs */
      }
      if (rerankerMode === "custom") {
        try {
          await saveRerankerSettings({
            provider: rerankerProvider,
            base_url: rerankerBaseUrl,
            api_key: rerankerApiKey,
            model: rerankerModel,
            enabled: true,
          });
        } catch {
          /* ignore */
        }
      }

      onCreated(kb);
    } catch (err) {
      toastApiError(err, onByokRedirect);
    } finally {
      setCreating(false);
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="presentation"
      onClick={() => !creating && onClose()}
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-xl rounded-2xl border bg-bg shadow-lift"
        role="dialog"
        aria-modal="true"
      >
        <header className="flex h-12 items-center justify-between border-b px-5">
          <h2 className="text-base font-semibold">新建知识库</h2>
          <button
            onClick={onClose}
            disabled={creating}
            className="rounded-md p-1 text-muted hover:bg-surface hover:text-fg"
            aria-label="关闭"
            type="button"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <form
          onSubmit={submit}
          className="max-h-[70vh] overflow-y-auto p-5 space-y-5"
        >
          <FormField label="名称" required>
            <input
              required
              maxLength={128}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如：我的笔记"
              className={inputClass}
            />
          </FormField>

          <FormField label="描述（可选）">
            <input
              maxLength={512}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="一句话说明这个 KB 的用途"
              className={inputClass}
            />
          </FormField>

          {/* v3-M8: embedding always required, no inherit/custom toggle */}
          <section className="rounded-xl border bg-surface/40 p-4 space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">Embedding 配置</h3>
              {embedDim != null && (
                <span className="chip border-border bg-surface text-xs text-muted">
                  {embedModel} · {embedDim}d
                </span>
              )}
            </div>
            <p className="text-xs text-muted">
              每个 KB 独立配置 embedding 提供商。第一次填后，后续建 KB 会自动预填上次的设置。
            </p>
            <div className="grid grid-cols-2 gap-2 pt-1">
              <select
                value={embedProvider}
                onChange={(e) => {
                  setEmbedProvider(e.target.value as EmbeddingProvider);
                  setEmbedVerified(false);
                  // v3-M8.3: provider drift → backend fall-back no longer
                  // applies, force the user to re-enter (or keep typing) a key.
                  setEmbedKeyEditing(true);
                }}
                className={inputClass}
              >
                <option value="openai-compat">OpenAI-compat</option>
                <option value="ollama">Ollama</option>
              </select>
              <input
                placeholder="https://api.siliconflow.cn/v1"
                value={embedBaseUrl}
                onChange={(e) => {
                  setEmbedBaseUrl(e.target.value);
                  setEmbedVerified(false);
                  // v3-M8.3: same — base_url drift breaks backend fall-back.
                  setEmbedKeyEditing(true);
                }}
                className={inputClass}
              />
            </div>
            {/* v3-M8.3: saved-key chip vs input toggle. When the user already
                saved an api_key in a prior KB creation, we show a compact
                read-only chip instead of an empty password field — the
                "已保存（留空保持现有）" placeholder was misleading. Click
                "修改" to swap to input mode for a new key. */}
            {embedKeySaved && !embedKeyEditing ? (
              <div className="flex items-center gap-2 rounded-lg border bg-surface/50 px-3 py-2 text-sm">
                <KeyRound className="h-4 w-4 text-success" />
                <span className="flex-1">已使用保存的 API Key</span>
                <button
                  type="button"
                  onClick={() => {
                    setEmbedKeyEditing(true);
                    setEmbedVerified(false);
                  }}
                  className="text-xs text-accent hover:underline"
                >
                  修改
                </button>
              </div>
            ) : (
              <div className="relative">
                <KeyRound className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                <input
                  type="password"
                  placeholder={
                    embedProvider === "ollama"
                      ? "API Key（ollama 可留空）"
                      : "API Key"
                  }
                  value={embedApiKey}
                  onChange={(e) => {
                    setEmbedApiKey(e.target.value);
                    setEmbedVerified(false);
                  }}
                  className={cn(inputClass, "pl-8")}
                  autoFocus={embedKeyEditing && embedKeySaved}
                />
                {embedKeySaved && (
                  <button
                    type="button"
                    onClick={() => {
                      setEmbedKeyEditing(false);
                      setEmbedApiKey("");
                      setEmbedVerified(true);  // back to using saved key
                    }}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-muted hover:text-fg"
                  >
                    取消
                  </button>
                )}
              </div>
            )}
            {/* v3-M8.1: 测试连接 — must succeed before model dropdown populates */}
            <div className="flex justify-end">
              <button
                type="button"
                onClick={onTestEmbedding}
                disabled={embedProbing || !embedBaseUrl}
                className="btn btn-ghost btn-sm"
              >
                {embedProbing ? "测试中…" : "测试连接 / 拉取模型"}
              </button>
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted">模型</label>
              {embedModels.length > 0 ? (
                <select
                  value={embedModel}
                  onChange={(e) => {
                    setEmbedModel(e.target.value);
                    setEmbedDim(null);  // model changed → invalidate dim, re-test
                    setEmbedVerified(false);
                  }}
                  className={inputClass}
                >
                  {/* Allow current value even if not in returned list */}
                  {!embedModels.includes(embedModel) && embedModel && (
                    <option value={embedModel}>{embedModel}（自定义）</option>
                  )}
                  {embedModels.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  placeholder="先点「测试连接」拉模型列表 — 或手输 model id"
                  value={embedModel}
                  onChange={(e) => {
                    setEmbedModel(e.target.value);
                    setEmbedDim(null);
                    setEmbedVerified(false);
                  }}
                  className={inputClass}
                />
              )}
              {embedDim != null && (
                <p className="mt-1 text-xs text-muted">
                  向量维度：<span className="text-fg">{embedDim}</span>
                </p>
              )}
              {embedDim == null && embedModels.length > 0 && (
                <p className="mt-1 text-xs text-warning">
                  请再次点「测试连接」探测当前模型的向量维度
                </p>
              )}
            </div>
          </section>

          {/* Reranker section — keep as "off / custom" collapsible */}
          <CollapsibleSection
            title="Reranker（重排序，可选）"
            badge={rerankerMode === "off" ? "不启用" : rerankerModel}
            badgeVariant="default"
            expanded={rerankerExpanded}
            onToggle={() => setRerankerExpanded((v) => !v)}
          >
            <div className="space-y-3">
              <RadioRow
                checked={rerankerMode === "off"}
                onChange={() => setRerankerMode("off")}
                label="不启用"
                hint="默认。embedding + 检索通常已经够用。"
              />
              <RadioRow
                checked={rerankerMode === "custom"}
                onChange={() => setRerankerMode("custom")}
                label="为这个 KB 启用 reranker"
                hint="开启后这个 KB 的检索结果会经过 cross-encoder 重排（额外 100-300ms / 次）。"
              />

              {rerankerMode === "custom" && (
                <div className="space-y-2 rounded-lg border bg-surface/30 p-3">
                  <div className="grid grid-cols-2 gap-2">
                    <select
                      value={rerankerProvider}
                      onChange={(e) => {
                        setRerankerProvider(
                          e.target.value as RerankerProvider
                        );
                        setRerankerVerified(false);
                        setRerankerKeyEditing(true);
                      }}
                      className={inputClass}
                    >
                      <option value="siliconflow">SiliconFlow</option>
                      <option value="cohere">Cohere</option>
                      <option value="openai-compat">OpenAI-compat</option>
                    </select>
                    <input
                      placeholder="https://api.siliconflow.cn/v1"
                      value={rerankerBaseUrl}
                      onChange={(e) => {
                        setRerankerBaseUrl(e.target.value);
                        setRerankerVerified(false);
                        setRerankerKeyEditing(true);
                      }}
                      className={inputClass}
                    />
                  </div>
                  {/* v3-M8.3: same saved-key chip vs input pattern as embedding. */}
                  {rerankerKeySaved && !rerankerKeyEditing ? (
                    <div className="flex items-center gap-2 rounded-lg border bg-surface/50 px-3 py-2 text-sm">
                      <KeyRound className="h-4 w-4 text-success" />
                      <span className="flex-1">已使用保存的 API Key</span>
                      <button
                        type="button"
                        onClick={() => {
                          setRerankerKeyEditing(true);
                          setRerankerVerified(false);
                        }}
                        className="text-xs text-accent hover:underline"
                      >
                        修改
                      </button>
                    </div>
                  ) : (
                    <div className="relative">
                      <KeyRound className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                      <input
                        type="password"
                        placeholder="API Key（自托管可留空）"
                        value={rerankerApiKey}
                        onChange={(e) => {
                          setRerankerApiKey(e.target.value);
                          setRerankerVerified(false);
                        }}
                        className={cn(inputClass, "pl-8")}
                        autoFocus={rerankerKeyEditing && rerankerKeySaved}
                      />
                      {rerankerKeySaved && (
                        <button
                          type="button"
                          onClick={() => {
                            setRerankerKeyEditing(false);
                            setRerankerApiKey("");
                            setRerankerVerified(true);
                          }}
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-muted hover:text-fg"
                        >
                          取消
                        </button>
                      )}
                    </div>
                  )}
                  <div className="flex justify-end">
                    <button
                      type="button"
                      onClick={onTestReranker}
                      disabled={rerankerProbing || !rerankerBaseUrl}
                      className="btn btn-ghost btn-sm"
                    >
                      {rerankerProbing ? "测试中…" : "测试连接 / 拉取模型"}
                    </button>
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-muted">模型</label>
                    {rerankerModels.length > 0 ? (
                      <select
                        value={rerankerModel}
                        onChange={(e) => {
                          setRerankerModel(e.target.value);
                          setRerankerVerified(false);
                        }}
                        className={inputClass}
                      >
                        {!rerankerModels.includes(rerankerModel) && rerankerModel && (
                          <option value={rerankerModel}>
                            {rerankerModel}（自定义）
                          </option>
                        )}
                        {rerankerModels.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        placeholder="先点「测试连接」拉模型列表 — 或手输 model id"
                        value={rerankerModel}
                        onChange={(e) => {
                          setRerankerModel(e.target.value);
                          setRerankerVerified(false);
                        }}
                        className={inputClass}
                      />
                    )}
                  </div>
                </div>
              )}
            </div>
          </CollapsibleSection>

          <div className="flex items-center justify-between gap-2 border-t pt-4">
            {!embedVerified ? (
              <span className="text-xs text-warning">
                ⚠ 请先点「测试连接 / 拉取模型」验证 Embedding 可用
              </span>
            ) : rerankerMode === "custom" && !rerankerVerified ? (
              <span className="text-xs text-warning">
                ⚠ 请先验证 Reranker 连接
              </span>
            ) : (
              <span />
            )}
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onClose}
                disabled={creating}
                className="btn btn-ghost btn-sm"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={
                  creating ||
                  !name.trim() ||
                  !embedVerified ||
                  (rerankerMode === "custom" && !rerankerVerified)
                }
                className="btn btn-primary btn-sm"
              >
                <Plus className="h-4 w-4" />
                {creating ? "创建中…" : "创建知识库"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small UI helpers
// ---------------------------------------------------------------------------
const inputClass =
  "block w-full rounded-lg border bg-bg px-3 py-2 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20";

function FormField({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-xs font-medium text-fg/80">
        {label}
        {required && <span className="ml-1 text-danger">*</span>}
      </label>
      {children}
    </div>
  );
}

function CollapsibleSection({
  title,
  badge,
  badgeVariant = "default",
  expanded,
  onToggle,
  children,
}: {
  title: string;
  badge?: string;
  badgeVariant?: "default" | "warning";
  expanded: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border bg-surface/40">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-2 px-4 py-3 text-left"
      >
        <span className="text-sm font-medium">{title}</span>
        <span className="flex items-center gap-2">
          {badge && (
            <span
              className={cn(
                "chip text-xs",
                badgeVariant === "warning"
                  ? "border-warning/30 bg-warning/10 text-warning"
                  : "border-border bg-surface text-muted"
              )}
            >
              {badge}
            </span>
          )}
          {expanded ? (
            <ChevronDown className="h-4 w-4 text-muted" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted" />
          )}
        </span>
      </button>
      {expanded && <div className="border-t px-4 py-3">{children}</div>}
    </section>
  );
}

function RadioRow({
  checked,
  onChange,
  disabled,
  label,
  hint,
}: {
  checked: boolean;
  onChange: () => void;
  disabled?: boolean;
  label: string;
  hint?: string;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 transition",
        checked ? "bg-accent/10" : "hover:bg-surface-2",
        disabled && "cursor-not-allowed opacity-50"
      )}
    >
      <input
        type="radio"
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        className="mt-0.5"
      />
      <div className="flex-1 text-sm">
        <div>{label}</div>
        {hint && <div className="mt-0.5 text-xs text-muted">{hint}</div>}
      </div>
    </label>
  );
}
