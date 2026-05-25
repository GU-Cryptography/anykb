"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "@/components/ThemeProvider";
import { cn } from "@/lib/cn";

const OPTIONS = [
  { value: "light" as const, Icon: Sun, label: "亮色" },
  { value: "system" as const, Icon: Monitor, label: "跟随系统" },
  { value: "dark" as const, Icon: Moon, label: "暗色" },
];

export default function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  return (
    <div
      className={cn(
        "inline-flex items-center gap-0.5 rounded-lg border bg-surface p-0.5",
        className
      )}
      role="group"
      aria-label="主题切换"
    >
      {OPTIONS.map(({ value, Icon, label }) => {
        const active = theme === value;
        return (
          <button
            key={value}
            onClick={() => setTheme(value)}
            className={cn(
              "inline-flex h-6 w-6 items-center justify-center rounded-md transition",
              active
                ? "bg-bg text-fg shadow-soft"
                : "text-muted hover:text-fg"
            )}
            title={label}
            aria-label={label}
            aria-pressed={active}
            type="button"
          >
            <Icon className="h-3.5 w-3.5" />
          </button>
        );
      })}
    </div>
  );
}
