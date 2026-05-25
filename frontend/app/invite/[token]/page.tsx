"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Sparkles, Users, Eye, CheckCircle, AlertCircle, BookOpen } from "lucide-react";
import { toast } from "sonner";

import Brand, { APP_NAME } from "@/components/Brand";
import { getToken } from "@/lib/auth";
import {
  acceptInvitation,
  peekInvitation,
  type InvitationPreview,
} from "@/lib/kb-api";

/**
 * v2-M9: invitation landing page.
 *
 * Flow:
 *   1. If not logged in → redirect to /login?next=/invite/{token}
 *   2. If logged in → peekInvitation(token) to show KB name + role
 *   3. User clicks "接受邀请" → acceptInvitation(token) → redirect to /kbs/{kb_id}
 *
 * Error states (404 invalid / 410 expired / 410 exhausted) render a friendly
 * "链接已失效" panel with a back-to-KBs link.
 */
export default function InvitePage({
  params,
}: {
  params: { token: string };
}) {
  const { token } = params;
  const router = useRouter();

  const [preview, setPreview] = useState<InvitationPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [accepting, setAccepting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!getToken()) {
      router.replace(`/login?next=/invite/${token}`);
      return;
    }
    try {
      const p = await peekInvitation(token);
      setPreview(p);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token, router]);

  useEffect(() => {
    void load();
  }, [load]);

  const onAccept = async () => {
    setAccepting(true);
    try {
      const { kb_id } = await acceptInvitation(token);
      toast.success("已加入知识库");
      router.replace(`/kbs/${kb_id}`);
    } catch (e) {
      toast.error((e as Error).message);
      setError((e as Error).message);
      setAccepting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4 text-fg">
      <div className="w-full max-w-md">
        <div className="mb-6 flex flex-col items-center gap-3">
          <Brand size="md" showWordmark={false} />
          <h1 className="text-lg font-semibold">{APP_NAME} 知识库邀请</h1>
        </div>

        <div className="rounded-2xl border bg-surface p-6">
          {loading ? (
            <div className="flex flex-col items-center gap-2 py-8 text-sm text-muted">
              <Sparkles className="h-5 w-5 animate-pulse text-accent" />
              加载中…
            </div>
          ) : error ? (
            <div className="flex flex-col items-center gap-3 py-4 text-center">
              <AlertCircle className="h-8 w-8 text-danger" />
              <div className="text-sm font-medium">链接无法使用</div>
              <div className="text-xs text-muted">{error}</div>
              <Link
                href="/kbs"
                className="btn btn-ghost btn-sm mt-2"
              >
                返回知识库列表
              </Link>
            </div>
          ) : preview ? (
            <div className="flex flex-col items-center gap-3 py-2 text-center">
              <BookOpen className="h-8 w-8 text-accent" />
              <div className="text-base font-medium">
                邀请你加入「{preview.kb_name}」
              </div>
              <div className="flex items-center gap-2 text-sm text-muted">
                你将获得
                {preview.role === "editor" ? (
                  <span className="chip border-info/30 bg-info/10 text-info">
                    <Users className="h-3 w-3" />
                    editor（读 + 写文档）
                  </span>
                ) : (
                  <span className="chip border-border bg-surface text-muted">
                    <Eye className="h-3 w-3" />
                    viewer（只读）
                  </span>
                )}
                权限
              </div>
              {preview.max_uses != null && (
                <div className="text-xs text-muted">
                  本链接已被使用 {preview.uses_count}/{preview.max_uses} 次
                </div>
              )}
              {preview.expires_at && (
                <div className="text-xs text-muted">
                  有效期至 {new Date(preview.expires_at).toLocaleString()}
                </div>
              )}

              <div className="mt-4 flex w-full gap-2">
                <Link
                  href="/kbs"
                  className="btn btn-ghost flex-1 justify-center"
                >
                  取消
                </Link>
                <button
                  onClick={onAccept}
                  disabled={accepting}
                  className="btn btn-primary flex-1 justify-center"
                  type="button"
                >
                  <CheckCircle className="h-4 w-4" />
                  {accepting ? "处理中…" : "接受邀请"}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
