"use client";

/**
 * v3-M5: System Settings Dialog — multi-tab modal mirrored after DeepSeek.
 *
 * Tabs:
 *   1. 通用     — edit display_name + theme toggle
 *   2. 账号     — email (read-only) + change password
 *   3. 数据     — export conversations / clear conversations / delete account
 *   4. 关于     — version + project links + MIT license
 *
 * The original /settings page (LLM / embedding / reranker provider credentials)
 * stays untouched — this dialog is purely the account-level UX surface.
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  X,
  User as UserIcon,
  KeyRound,
  Database,
  Info,
  Download,
  Trash2,
  AlertTriangle,
} from "lucide-react";
import { toast } from "sonner";

import Dialog from "@/components/Dialog";
import ThemeToggle from "@/components/ThemeToggle";
import {
  changePassword,
  deleteAccount,
  logout,
  updateProfile,
  type User,
} from "@/lib/auth";
import {
  deleteAllConversations,
  exportConversations,
} from "@/lib/conversations-api";
import { cn } from "@/lib/cn";

type Tab = "general" | "account" | "data" | "about";

const TABS: { key: Tab; label: string; Icon: typeof UserIcon }[] = [
  { key: "general", label: "通用", Icon: UserIcon },
  { key: "account", label: "账号", Icon: KeyRound },
  { key: "data", label: "数据", Icon: Database },
  { key: "about", label: "关于", Icon: Info },
];

type Props = {
  open: boolean;
  onClose: () => void;
  user: User;
  onUserChanged: (u: User) => void;
};

export default function SystemSettingsDialog({
  open,
  onClose,
  user,
  onUserChanged,
}: Props) {
  const [tab, setTab] = useState<Tab>("general");
  const panelRef = useRef<HTMLDivElement>(null);

  // ESC + body scroll lock
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const orig = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = orig;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      onClick={onClose}
      role="presentation"
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
      <div
        ref={panelRef}
        onClick={(e) => e.stopPropagation()}
        className="relative flex h-[560px] w-full max-w-2xl overflow-hidden rounded-2xl border bg-bg shadow-lift"
        role="dialog"
        aria-modal="true"
        aria-labelledby="syssettings-title"
      >
        {/* Left tab nav */}
        <nav className="w-44 shrink-0 border-r bg-surface/40 p-2">
          <div className="px-2 py-2 text-xs font-semibold uppercase tracking-wider text-muted">
            系统设置
          </div>
          {TABS.map(({ key, label, Icon }) => {
            const active = tab === key;
            return (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-2 py-2 text-sm transition",
                  active
                    ? "bg-accent/15 text-fg"
                    : "text-fg/80 hover:bg-surface-2"
                )}
                type="button"
              >
                <Icon className="h-4 w-4 opacity-70" />
                {label}
              </button>
            );
          })}
        </nav>

        {/* Right content */}
        <div className="flex flex-1 flex-col">
          <header className="flex h-12 shrink-0 items-center justify-between border-b px-5">
            <h2 id="syssettings-title" className="text-base font-semibold">
              {TABS.find((t) => t.key === tab)?.label}
            </h2>
            <button
              onClick={onClose}
              className="rounded-md p-1 text-muted hover:bg-surface hover:text-fg"
              aria-label="关闭"
              type="button"
            >
              <X className="h-4 w-4" />
            </button>
          </header>

          <div className="flex-1 overflow-y-auto p-5">
            {tab === "general" && (
              <GeneralTab user={user} onUserChanged={onUserChanged} />
            )}
            {tab === "account" && <AccountTab user={user} />}
            {tab === "data" && <DataTab onClose={onClose} />}
            {tab === "about" && <AboutTab />}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: 通用
// ---------------------------------------------------------------------------
function GeneralTab({
  user,
  onUserChanged,
}: {
  user: User;
  onUserChanged: (u: User) => void;
}) {
  const initialName =
    user.display_name?.trim() || user.email.split("@")[0];
  const [name, setName] = useState(initialName);
  const [saving, setSaving] = useState(false);

  const dirty = name.trim() !== initialName && name.trim().length >= 1;

  const save = async () => {
    const v = name.trim();
    if (!v) {
      toast.error("名称不能为空");
      return;
    }
    setSaving(true);
    try {
      const updated = await updateProfile(v);
      onUserChanged(updated);
      toast.success("已保存");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <Field label="显示名称" hint="出现在侧边栏底部、对话署名等位置">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={64}
          placeholder={user.email.split("@")[0]}
          className={inputClass}
        />
        <div className="mt-2 flex justify-end">
          <button
            onClick={save}
            disabled={!dirty || saving}
            className="btn btn-primary btn-sm"
            type="button"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </Field>

      <Field label="主题">
        <ThemeToggle />
        <p className="mt-2 text-xs text-muted">
          跟随系统会自动切换 — 也可以手动指定亮色 / 暗色。
        </p>
      </Field>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: 账号
// ---------------------------------------------------------------------------
function AccountTab({ user }: { user: User }) {
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    if (newPw.length < 8) {
      toast.error("新密码至少 8 位");
      return;
    }
    if (newPw !== confirmPw) {
      toast.error("两次输入的新密码不一致");
      return;
    }
    setSaving(true);
    try {
      await changePassword(oldPw, newPw);
      toast.success("密码已更新");
      setOldPw("");
      setNewPw("");
      setConfirmPw("");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "修改失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <Field label="邮箱（登录账号）">
        <input
          type="email"
          value={user.email}
          readOnly
          className={cn(inputClass, "cursor-not-allowed bg-surface text-muted")}
        />
        <p className="mt-2 text-xs text-muted">邮箱目前不支持修改。</p>
      </Field>

      <div className="border-t pt-5">
        <h3 className="mb-3 text-sm font-medium">修改密码</h3>
        <div className="space-y-3">
          <Field label="当前密码">
            <input
              type="password"
              value={oldPw}
              onChange={(e) => setOldPw(e.target.value)}
              className={inputClass}
              autoComplete="current-password"
            />
          </Field>
          <Field label="新密码（至少 8 位）">
            <input
              type="password"
              value={newPw}
              onChange={(e) => setNewPw(e.target.value)}
              className={inputClass}
              autoComplete="new-password"
              minLength={8}
            />
          </Field>
          <Field label="再次输入新密码">
            <input
              type="password"
              value={confirmPw}
              onChange={(e) => setConfirmPw(e.target.value)}
              className={inputClass}
              autoComplete="new-password"
            />
          </Field>
          <div className="flex justify-end pt-1">
            <button
              onClick={submit}
              disabled={!oldPw || !newPw || saving}
              className="btn btn-primary btn-sm"
              type="button"
            >
              {saving ? "提交中…" : "更新密码"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: 数据
// ---------------------------------------------------------------------------
function DataTab({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [exporting, setExporting] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const doExport = async () => {
    setExporting(true);
    try {
      await exportConversations();
      toast.success("已开始下载");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导出失败");
    } finally {
      setExporting(false);
    }
  };

  const doClear = async () => {
    setClearing(true);
    try {
      await deleteAllConversations();
      toast.success("已清空所有对话");
      setConfirmClear(false);
      // Reload so the sidebar refreshes from server
      window.location.reload();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "清空失败");
      setClearing(false);
    }
  };

  const doDelete = async () => {
    setDeleting(true);
    try {
      await deleteAccount();
      toast.success("账号已删除");
      onClose();
      router.replace("/login");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
      setDeleting(false);
    }
  };

  return (
    <div className="space-y-5">
      <DataRow
        title="导出对话历史"
        description="下载所有对话 + 消息为 JSON，方便迁移或本地备份。"
        action={
          <button
            onClick={doExport}
            disabled={exporting}
            className="btn btn-ghost btn-sm"
            type="button"
          >
            <Download className="h-3.5 w-3.5" />
            {exporting ? "导出中…" : "导出 JSON"}
          </button>
        }
      />

      <DataRow
        title="清空所有对话"
        description="不可恢复，但 KB / 账号配置不受影响。"
        action={
          <button
            onClick={() => setConfirmClear(true)}
            className="btn btn-ghost btn-sm text-danger hover:bg-danger/10"
            type="button"
          >
            <Trash2 className="h-3.5 w-3.5" />
            清空对话
          </button>
        }
      />

      <div className="rounded-xl border border-danger/30 bg-danger/5 p-4">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 flex-none text-danger" />
          <div className="flex-1">
            <h3 className="text-sm font-medium text-danger">删除账号</h3>
            <p className="mt-1 text-xs text-muted">
              永久删除账号、所有对话、所拥有的知识库以及上传的文档。<strong>不可恢复。</strong>
            </p>
            <button
              onClick={() => setConfirmDelete(true)}
              className="btn btn-danger btn-sm mt-3"
              type="button"
            >
              删除我的账号
            </button>
          </div>
        </div>
      </div>

      <Dialog
        open={confirmClear}
        onOpenChange={setConfirmClear}
        title="清空所有对话？"
        description="所有对话历史将被永久删除，无法恢复。KB 和账号设置不受影响。"
        variant="danger"
        confirmLabel="确认清空"
        onConfirm={doClear}
        busy={clearing}
      />

      <Dialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="删除账号？"
        description="账号 + 对话 + 我所拥有的 KB + 上传文档都会被永久删除。此操作不可恢复。"
        variant="danger"
        confirmLabel="确认删除账号"
        onConfirm={doDelete}
        busy={deleting}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: 关于
// ---------------------------------------------------------------------------
function AboutTab() {
  return (
    <div className="space-y-4 text-sm">
      <div>
        <h3 className="font-medium">AnyKB</h3>
        <p className="mt-1 text-muted">
          Your personal RAG chat over any knowledge base.
        </p>
      </div>

      <dl className="grid grid-cols-[100px_1fr] gap-y-2 text-xs">
        <dt className="text-muted">版本</dt>
        <dd>v3-M5</dd>
        <dt className="text-muted">协议</dt>
        <dd>MIT</dd>
        <dt className="text-muted">仓库</dt>
        <dd className="break-all text-accent">
          <a
            href="https://github.com/GU-Cryptography/anykb"
            target="_blank"
            rel="noopener noreferrer"
            className="hover:underline"
          >
            github.com/GU-Cryptography/anykb
          </a>
        </dd>
      </dl>

      <div className="rounded-xl border bg-surface/40 p-4 text-xs text-muted">
        <p>
          AnyKB 是一个本地优先的 RAG 平台，所有数据保存在你自己的数据库中。
          使用本服务即表示你了解：LLM 输出可能不准确；上传到知识库的文档会经过
          embedding 提供商处理。详见仓库 README。
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------
const inputClass =
  "block w-full rounded-lg border bg-bg px-3 py-2 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-fg/80 mb-1.5">
        {label}
      </label>
      {children}
      {hint && <p className="mt-1.5 text-xs text-muted">{hint}</p>}
    </div>
  );
}

function DataRow({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 rounded-xl border bg-surface/40 p-4">
      <div className="flex-1">
        <h3 className="text-sm font-medium">{title}</h3>
        <p className="mt-1 text-xs text-muted">{description}</p>
      </div>
      <div className="shrink-0">{action}</div>
    </div>
  );
}
