"use client";

import { useRouter } from "next/navigation";
import { useState, FormEvent } from "react";
import { ArrowRight, LogIn, Mail, Lock, ChevronLeft } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

import Brand, { APP_NAME } from "@/components/Brand";
import BrandPanel from "@/components/BrandPanel";
import { login } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await login(email.trim(), password);
      const next = new URLSearchParams(window.location.search).get("next");
      const safeNext = next && next.startsWith("/") ? next : "/";
      router.replace(safeNext);
    } catch (err: unknown) {
      toast.error((err as Error)?.message ?? "登录失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      {/* Left brand panel — hidden on mobile */}
      <BrandPanel />

      {/* Right form panel */}
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
            <h1 className="text-2xl font-bold">欢迎回到 {APP_NAME}</h1>
            <p className="mt-2 text-sm text-muted">登录开始管理你的知识库</p>

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
                <label className="text-xs font-medium text-fg">密码</label>
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                  <input
                    type="password"
                    autoComplete="current-password"
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
                  "登录中..."
                ) : (
                  <>
                    <LogIn className="h-4 w-4" />
                    登录
                  </>
                )}
              </button>
            </form>

            <p className="mt-6 text-center text-sm text-muted">
              还没有账号？{" "}
              <Link
                href="/register"
                className="inline-flex items-center gap-0.5 font-medium text-accent hover:underline"
              >
                免费注册 <ArrowRight className="h-3 w-3" />
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
