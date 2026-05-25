"use client";

import { useRouter } from "next/navigation";
import { useState, FormEvent } from "react";
import { ArrowRight, UserPlus, Mail, Lock, User as UserIcon, ChevronLeft } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

import Brand, { APP_NAME } from "@/components/Brand";
import BrandPanel from "@/components/BrandPanel";
import { register } from "@/lib/auth";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [loading, setLoading] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await register(email.trim(), password, displayName.trim());
      const next = new URLSearchParams(window.location.search).get("next");
      const safeNext = next && next.startsWith("/") ? next : "/";
      router.replace(safeNext);
    } catch (err: unknown) {
      toast.error((err as Error)?.message ?? "注册失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      <BrandPanel />

      <div className="flex min-h-screen flex-col bg-bg px-6 py-8 sm:px-10">
        <div className="flex items-center justify-between">
          <Link
            href="/welcome"
            className="inline-flex items-center gap-1 text-sm text-muted transition hover:text-fg"
          >
            <ChevronLeft className="h-4 w-4" />
            返回首页
          </Link>
          <div className="flex items-center gap-2 lg:hidden">
            <Brand size="sm" showWordmark />
          </div>
        </div>

        <div className="flex flex-1 items-center justify-center">
          <div className="w-full max-w-sm">
            <h1 className="text-2xl font-bold">创建 {APP_NAME} 账号</h1>
            <p className="mt-2 text-sm text-muted">
              免费 · 数据存在你自己的机器上
            </p>

            <form onSubmit={onSubmit} className="mt-8 space-y-4">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-fg">邮箱</label>
                <div className="relative">
                  <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                  <input
                    type="email"
                    autoComplete="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="block w-full rounded-lg border bg-bg pl-9 pr-3 py-2.5 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                    placeholder="you@example.com"
                  />
                </div>
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-medium text-fg">
                  显示名 <span className="font-normal text-muted">（可选）</span>
                </label>
                <div className="relative">
                  <UserIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                  <input
                    type="text"
                    maxLength={64}
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    className="block w-full rounded-lg border bg-bg pl-9 pr-3 py-2.5 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                    placeholder="如何称呼你"
                  />
                </div>
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-medium text-fg">密码</label>
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                  <input
                    type="password"
                    autoComplete="new-password"
                    required
                    minLength={8}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="block w-full rounded-lg border bg-bg pl-9 pr-3 py-2.5 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                    placeholder="至少 8 位"
                  />
                </div>
              </div>

              <button
                type="submit"
                disabled={loading}
                className="btn btn-primary inline-flex w-full items-center justify-center py-2.5"
              >
                {loading ? (
                  "注册中..."
                ) : (
                  <>
                    <UserPlus className="h-4 w-4" />
                    免费注册
                  </>
                )}
              </button>

              <p className="text-center text-[11px] leading-relaxed text-muted">
                注册即代表你同意我们的{" "}
                <a href="#" className="underline hover:text-fg">
                  服务协议
                </a>{" "}
                和{" "}
                <a href="#" className="underline hover:text-fg">
                  隐私政策
                </a>
              </p>
            </form>

            <p className="mt-6 text-center text-sm text-muted">
              已有账号？{" "}
              <Link
                href="/login"
                className="inline-flex items-center gap-0.5 font-medium text-accent hover:underline"
              >
                去登录 <ArrowRight className="h-3 w-3" />
              </Link>
            </p>
          </div>
        </div>

        <p className="text-center text-xs text-muted">
          © {new Date().getFullYear()} {APP_NAME} · MIT License
        </p>
      </div>
    </div>
  );
}
