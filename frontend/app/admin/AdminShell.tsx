"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { ChevronLeft, LayoutDashboard, Users, BookOpen, Sparkles } from "lucide-react";

import { getToken, getUser, refreshMe } from "@/lib/auth";
import { cn } from "@/lib/cn";

/**
 * Client-side guard + chrome for the /admin/* pages (06-01).
 *
 * The guard is UX only — the backend 403 on every /api/admin/* call is the real
 * gate. On mount we refresh /api/auth/me so a freshly-granted is_admin flag is
 * picked up without re-login, then redirect non-admins home.
 */
export default function AdminShell({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    // Fast path off the cached user, then confirm against the server.
    if (getUser()?.is_admin === false) {
      router.replace("/");
      return;
    }
    let active = true;
    refreshMe()
      .then((u) => {
        if (!active) return;
        // Fall back to the cached user when /me is unreachable.
        const isAdmin = (u ?? getUser())?.is_admin;
        if (!isAdmin) {
          router.replace("/");
          return;
        }
        setReady(true);
      })
      .catch(() => {
        if (!active) return;
        if (!getUser()?.is_admin) router.replace("/");
        else setReady(true);
      });
    return () => {
      active = false;
    };
  }, [router]);

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center text-muted">
        <Sparkles className="mr-2 h-4 w-4 animate-pulse" />
        加载中…
      </div>
    );
  }

  const tabs = [
    { href: "/admin", label: "看板", icon: LayoutDashboard },
    { href: "/admin/users", label: "用户", icon: Users },
    { href: "/admin/kbs", label: "知识库", icon: BookOpen },
  ];

  return (
    <div className="min-h-screen bg-bg text-fg">
      <header className="border-b bg-bg/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-3 px-4 sm:px-6">
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-sm text-muted transition hover:text-fg"
          >
            <ChevronLeft className="h-4 w-4" />
            <span>返回对话</span>
          </Link>
          <div className="flex-1" />
          <h1 className="text-sm font-medium">{title}</h1>
        </div>
        <nav className="mx-auto flex max-w-5xl items-center gap-1 px-4 pb-2 sm:px-6">
          {tabs.map((t) => {
            const active = pathname === t.href;
            const Icon = t.icon;
            return (
              <Link
                key={t.href}
                href={t.href}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition",
                  active
                    ? "bg-accent/15 text-fg"
                    : "text-muted hover:bg-surface hover:text-fg"
                )}
              >
                <Icon className="h-4 w-4" />
                {t.label}
              </Link>
            );
          })}
        </nav>
      </header>

      <main className="mx-auto max-w-5xl px-4 py-6 sm:px-6">{children}</main>
    </div>
  );
}
