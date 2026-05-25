"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, CircleCheck, CircleAlert, LoaderCircle, Ban } from "lucide-react";

export type ToolEvent = {
  name: string;
  input?: Record<string, unknown>;
  status: "running" | "ok" | "error" | "blocked";
  latency_ms?: number | null;
  t0?: number;
  error?: string | null;
  reason?: string;
};

const NAME_LABEL: Record<string, string> = {
  // KB mode (generic search across user's knowledge base)
  search_kb: "📚 检索 KB",
  // General chat mode (v2-M5)
  web_search: "🌐 搜索网络",
  // KB mode report skill (v2-M8)
  generate_kb_report: "📝 生成 KB 报告",
  // Travel demo mode (preserved — only triggered by the system travel KB)
  get_weather: "🌤 查天气",
  search_restaurant_kb: "🍴 找本地餐厅",
  amap_search: "🗺 高德兜底",
  generate_travel_report: "✨ 生成旅行报告",
};

const STATUS_ICON: Record<ToolEvent["status"], React.ReactNode> = {
  running: <LoaderCircle className="h-3.5 w-3.5 animate-spin text-accent" />,
  ok: <CircleCheck className="h-3.5 w-3.5 text-accent" />,
  error: <CircleAlert className="h-3.5 w-3.5 text-red-500" />,
  blocked: <Ban className="h-3.5 w-3.5 text-amber-500" />,
};

export default function ThinkingChain({ events }: { events: ToolEvent[] }) {
  const [open, setOpen] = useState(true);
  const hasRunning = events.some((e) => e.status === "running");
  const [, force] = useState(0);

  // Tick every 200ms while something is running, so elapsed time updates live.
  useEffect(() => {
    if (!hasRunning) return;
    const id = setInterval(() => force((v) => v + 1), 200);
    return () => clearInterval(id);
  }, [hasRunning]);

  const doneCount = events.filter((e) => e.status !== "running").length;
  const summary = hasRunning
    ? `思考中 · ${doneCount}/${events.length} 步`
    : `思考过程 · ${events.length} 步`;

  return (
    <div className="rounded-xl border bg-surface">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-sm"
        type="button"
      >
        <span className="flex items-center gap-2 text-muted">
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          {hasRunning && <LoaderCircle className="h-3.5 w-3.5 animate-spin text-accent" />}
          {summary}
        </span>
      </button>
      {open && (
        <ul className="space-y-2 border-t p-3 text-sm">
          {events.map((e, i) => {
            const running = e.status === "running";
            const elapsed = running && e.t0 ? Math.max(0, Date.now() - e.t0) : null;
            return (
              <li key={i} className="flex items-start gap-2">
                {STATUS_ICON[e.status]}
                <div className="flex-1">
                  <div className="flex items-center justify-between">
                    <span className={running ? "text-fg" : "text-fg/80"}>
                      {NAME_LABEL[e.name] || e.name}
                    </span>
                    {running && elapsed != null && (
                      <span className="text-xs text-muted tabular-nums">
                        {(elapsed / 1000).toFixed(1)}s
                      </span>
                    )}
                    {!running && e.latency_ms != null && (
                      <span className="text-xs text-muted tabular-nums">{e.latency_ms}ms</span>
                    )}
                  </div>
                  {e.input && Object.keys(e.input).length > 0 && (
                    <pre className="mt-1 overflow-x-auto rounded bg-fg/5 p-2 text-xs text-muted">
                      {JSON.stringify(e.input, null, 0)}
                    </pre>
                  )}
                  {e.error && <p className="mt-1 text-xs text-red-500">{e.error}</p>}
                  {e.reason && <p className="mt-1 text-xs text-amber-600">⛔ {e.reason}</p>}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
