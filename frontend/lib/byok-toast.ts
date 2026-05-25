"use client";

import { toast } from "sonner";

import { KbApiError } from "./kb-api";

/**
 * v2-M2 BYOK error toasts. If the error is a structured BYOK gate response
 * (KbApiError with code = "llm_not_configured" / "embedding_not_configured"),
 * shows a sonner toast with a "去配置" action button that navigates to
 * /settings via the provided pusher. Otherwise falls back to plain toast.error.
 */
export function toastApiError(
  err: unknown,
  push: (path: string) => void,
): void {
  if (err instanceof KbApiError && err.code) {
    if (err.code === "llm_not_configured" || err.code === "embedding_not_configured") {
      toast.error(err.message, {
        action: {
          label: "去配置",
          onClick: () => push(err.settings_url ?? "/settings"),
        },
      });
      return;
    }
  }
  toast.error((err as Error)?.message ?? "操作失败");
}
