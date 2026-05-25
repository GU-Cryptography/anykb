"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";

type DialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "default" | "danger";
  onConfirm: () => void | Promise<void>;
  busy?: boolean;
};

/**
 * Minimal accessible modal dialog.
 *
 * Behavior:
 *   - ESC to dismiss (unless busy)
 *   - click backdrop to dismiss (unless busy)
 *   - body scroll locked while open
 *   - auto-focus the confirm button on open
 *
 * For richer behavior (focus trap, return focus, animations) swap in Radix
 * Dialog later — same prop surface.
 */
export default function Dialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "确定",
  cancelLabel = "取消",
  variant = "default",
  onConfirm,
  busy = false,
}: DialogProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onOpenChange(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onOpenChange, busy]);

  useEffect(() => {
    if (!open) return;
    const orig = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = orig;
    };
  }, [open]);

  useEffect(() => {
    if (open) {
      // Delay until after paint so the element is mounted.
      const t = window.setTimeout(() => confirmRef.current?.focus(), 0);
      return () => window.clearTimeout(t);
    }
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      onClick={() => !busy && onOpenChange(false)}
      role="presentation"
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-sm rounded-2xl border bg-bg p-5 shadow-lift"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
      >
        <button
          onClick={() => !busy && onOpenChange(false)}
          className="absolute right-3 top-3 rounded-md p-1 text-muted hover:bg-surface hover:text-fg"
          aria-label="关闭"
          type="button"
        >
          <X className="h-4 w-4" />
        </button>

        <h2 id="dialog-title" className="pr-6 text-base font-semibold">
          {title}
        </h2>
        {description && (
          <div className="mt-2 text-sm text-muted">{description}</div>
        )}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            disabled={busy}
            className="btn btn-ghost btn-sm"
            type="button"
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            onClick={onConfirm}
            disabled={busy}
            className={cn(
              "btn btn-sm",
              variant === "danger" ? "btn-danger" : "btn-primary"
            )}
            type="button"
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
