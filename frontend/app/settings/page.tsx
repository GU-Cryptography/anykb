"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, KeyRound, Loader2, Sparkles, Trash2 } from "lucide-react";
import { toast } from "sonner";

import Dialog from "@/components/Dialog";
import Select from "@/components/Select";
import { getToken } from "@/lib/auth";
import {
  clearLLMSettings,
  getMySettings,
  probeLLM,
  saveKbOptions,
  saveLLMSettings,
  SettingsApiError,
  type LLMProvider,
  type MyKbOptions,
  type MyLLMSettings,
  type MySettings,
} from "@/lib/settings-api";

/**
 * /settings — LLM provider credentials (v3-M8 simplified).
 *
 * Embedding + Reranker config has been removed from this page (v3-M8) — both
 * are now configured per-KB at creation time. This page exists solely to:
 *   - Save LLM provider creds (provider + base_url + api_key + default_model)
 *   - Toggle KB-mode options (e.g. web_search opt-in)
 *
 * The default LLM model saved here is the fallback for any conversation that
 * hasn't explicitly picked a model via the chat header Model selector (v3-M6).
 */
export default function SettingsPage() {
  const router = useRouter();
  const [settings, setSettings] = useState<MySettings | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    const s = await getMySettings();
    setSettings(s);
    return s;
  };

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    refresh()
      .catch((e) => toast.error((e as Error).message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center text-muted">
        <Sparkles className="mr-2 h-4 w-4 animate-pulse" />
        加载中…
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-6 flex items-center gap-2">
        <Link
          href="/"
          className="inline-flex items-center gap-1 rounded-md p-1 text-sm text-muted transition hover:bg-surface hover:text-fg"
        >
          <ArrowLeft className="h-4 w-4" />
          返回
        </Link>
      </div>

      <h1 className="text-2xl font-semibold">模型设置</h1>
      <p className="mt-1 text-sm text-muted">
        配置 LLM 提供商凭据（用于聊天）。Embedding 和 Reranker 现在在创建知识库时按 KB 单独配置。
      </p>

      <div className="mt-6 space-y-6">
        <LLMCard
          initial={settings?.llm}
          onChanged={refresh}
        />
        <KbOptionsCard
          initial={settings?.kb_options}
          onChanged={refresh}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// LLM card
// ---------------------------------------------------------------------------
function LLMCard({
  initial,
  onChanged,
}: {
  initial?: MyLLMSettings;
  onChanged: () => Promise<MySettings>;
}) {
  const [provider, setProvider] = useState<LLMProvider>(
    (initial?.provider as LLMProvider) || "openai-compat"
  );
  const [baseUrl, setBaseUrl] = useState(initial?.base_url || "");
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState(initial?.default_model || "");
  const [complexModel, setComplexModel] = useState(initial?.complex_model || "");
  const [probing, setProbing] = useState(false);
  const [saving, setSaving] = useState(false);
  const hasSavedKey = initial?.has_key ?? false;

  const placeholders = useMemo(() => {
    if (provider === "anthropic")
      return { url: "https://api.anthropic.com", key: "sk-ant-..." };
    return { url: "https://api.deepseek.com   ←  或 OpenAI / vLLM / LMStudio", key: "sk-..." };
  }, [provider]);

  // Allow saving without re-entering key (user wants to change model only).
  const effectiveKey = apiKey || (hasSavedKey ? "" : "");
  const canProbe = baseUrl.trim() && apiKey.trim() && !probing;
  const canSave =
    !!defaultModel &&
    (apiKey.trim() || hasSavedKey) &&
    !!baseUrl.trim() &&
    !saving;

  async function handleProbe() {
    if (!canProbe) return;
    setProbing(true);
    try {
      const r = await probeLLM({
        provider,
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
      });
      setModels(r.models);
      if (r.models.length === 0) {
        toast.warning("已连接，但服务端没返回任何模型");
      } else {
        toast.success(`发现 ${r.models.length} 个模型`);
      }
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setProbing(false);
    }
  }

  async function handleSave() {
    if (!canSave) return;
    setSaving(true);
    try {
      await saveLLMSettings({
        provider,
        base_url: baseUrl.trim(),
        api_key: apiKey,
        default_model: defaultModel,
        complex_model: complexModel,
      });
      toast.success("LLM 配置已保存");
      setApiKey("");
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function handleClear() {
    if (!confirm("清除 LLM 配置 → 回落到系统默认。继续？")) return;
    try {
      await clearLLMSettings();
      toast.success("已清除");
      setApiKey("");
      setModels([]);
      setDefaultModel("");
      setComplexModel("");
      await onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  // Pre-fill model options with the saved model so the user can submit
  // without re-probing (e.g. they just want to update the URL).
  const modelOptions = useMemo(() => {
    const merged = new Set(models);
    if (defaultModel) merged.add(defaultModel);
    if (complexModel) merged.add(complexModel);
    return Array.from(merged)
      .sort()
      .map((m) => ({ value: m, label: m }));
  }, [models, defaultModel, complexModel]);

  return (
    <section className="card p-5">
      <header className="mb-4 flex items-start justify-between gap-2">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Sparkles className="h-4 w-4 text-accent" />
            LLM 提供商
          </h2>
          <p className="mt-1 text-xs text-muted">
            {initial?.configured
              ? `当前：${initial.provider} · ${initial.default_model}`
              : "未配置，使用系统默认"}
          </p>
        </div>
        {initial?.configured && (
          <button
            onClick={handleClear}
            className="btn btn-ghost btn-sm text-muted"
            type="button"
          >
            <Trash2 className="h-3.5 w-3.5" />
            清除
          </button>
        )}
      </header>

      <div className="space-y-3 text-sm">
        <Field label="Provider">
          <Select
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value as LLMProvider);
              setModels([]);
            }}
            options={[
              { value: "anthropic", label: "anthropic（Claude）" },
              { value: "openai-compat", label: "openai-compat（OpenAI / DeepSeek / vLLM / 任意）" },
            ]}
          />
        </Field>

        <Field label="Base URL">
          <input
            type="url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder={placeholders.url}
            className="block w-full rounded-lg border bg-bg px-3 py-2 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
          />
        </Field>

        <Field label="API Key">
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <KeyRound className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={
                  hasSavedKey ? "已保存（留空保持现有）" : placeholders.key
                }
                className="block w-full rounded-lg border bg-bg pl-8 pr-3 py-2 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
              />
            </div>
            <button
              onClick={handleProbe}
              disabled={!canProbe}
              className="btn btn-ghost btn-sm"
              type="button"
            >
              {probing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                "测试连接"
              )}
            </button>
          </div>
        </Field>

        <Field label="Default Model">
          <Select
            value={defaultModel}
            onChange={(e) => setDefaultModel(e.target.value)}
            options={modelOptions}
            disabled={modelOptions.length === 0}
            placeholderOption={
              modelOptions.length === 0
                ? { value: "", label: "请先点击测试连接" }
                : { value: "", label: "请选择…" }
            }
            className="min-w-[16rem]"
          />
        </Field>

        <Field label="Complex Model（可选，用于复杂任务）">
          <Select
            value={complexModel}
            onChange={(e) => setComplexModel(e.target.value)}
            options={modelOptions}
            disabled={modelOptions.length === 0}
            placeholderOption={{ value: "", label: "与 Default 相同" }}
            className="min-w-[16rem]"
          />
        </Field>
      </div>

      <div className="mt-4 flex justify-end">
        <button
          onClick={handleSave}
          disabled={!canSave}
          className="btn btn-primary"
          type="button"
        >
          {saving ? "保存中…" : "保存"}
        </button>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// KbOptions card (v2-M6) — KB-mode toggles
// ---------------------------------------------------------------------------
function KbOptionsCard({
  initial,
  onChanged,
}: {
  initial?: MyKbOptions;
  onChanged: () => Promise<MySettings>;
}) {
  const [webEnabled, setWebEnabled] = useState<boolean>(
    initial?.kb_web_search_enabled ?? false
  );
  const [saving, setSaving] = useState(false);

  // Sync from server state if parent refetches.
  useEffect(() => {
    setWebEnabled(initial?.kb_web_search_enabled ?? false);
  }, [initial?.kb_web_search_enabled]);

  const dirty = webEnabled !== (initial?.kb_web_search_enabled ?? false);

  const onSave = async () => {
    setSaving(true);
    try {
      await saveKbOptions({ kb_web_search_enabled: webEnabled });
      await onChanged();
      toast.success("已保存");
    } catch (e) {
      toast.error((e as Error).message || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="card p-5">
      <header className="mb-3 flex items-center gap-2">
        <KeyRound className="h-4 w-4 text-accent" />
        <h2 className="text-base font-semibold">KB 模式选项</h2>
      </header>

      <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-border p-3 transition hover:bg-surface">
        <input
          type="checkbox"
          checked={webEnabled}
          onChange={(e) => setWebEnabled(e.target.checked)}
          disabled={saving}
          className="mt-0.5 h-4 w-4 accent-accent"
        />
        <div className="flex-1">
          <div className="text-sm font-medium">KB 对话允许调用网络搜索作为兜底</div>
          <p className="mt-1 text-xs text-muted leading-relaxed">
            开启后，绑定 KB 的对话里 agent 仍然优先 <code className="rounded bg-surface px-1">search_kb</code>{" "}
            检索你的文档；只在 KB 没有相关 chunks（相关度 &lt; 0.4）时，**最多调一次**{" "}
            <code className="rounded bg-surface px-1">web_search</code> 兜底补充。
            答案会按【📚 KB】/【🌐 Web】分段标注来源。默认关闭以保持答案严格基于知识库。
          </p>
        </div>
      </label>

      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={onSave}
          disabled={saving || !dirty}
          className="btn btn-primary btn-sm"
        >
          {saving ? (
            <>
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              保存中…
            </>
          ) : (
            "保存"
          )}
        </button>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 text-xs font-medium text-muted">{label}</div>
      {children}
    </label>
  );
}
