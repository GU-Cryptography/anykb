"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Download, Copy, X, Loader2, Image as ImageIcon, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { APP_NAME } from "@/components/Brand";

type Props = {
  open: boolean;
  onClose: () => void;
  markdown: string;
  /** Optional — the user question that triggered this answer (rendered as card sub-heading). */
  question?: string;
};

/**
 * Share card dialog (v3.1).
 *
 * Renders the assistant answer into a brand-framed card optimized for social
 * sharing (WeChat moments / Twitter image / Slack screenshot). The DOM is
 * rasterized to a PNG via `html2canvas` so users can download or paste it
 * straight to their chat app. Every shared card carries the `{APP_NAME}` mark
 * + tagline at top and a watermark at bottom — turning every share into a
 * product impression.
 *
 * Why not html2pdf: a PDF needs a reader and doesn't paste into chat. PNG is
 * the universal "image you can drop anywhere" format.
 */
export default function ShareCardDialog({
  open,
  onClose,
  markdown,
  question,
}: Props) {
  const cardRef = useRef<HTMLDivElement>(null);
  const [rendering, setRendering] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // ESC closes
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !rendering) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, rendering]);

  if (!open || !mounted) return null;

  const toCanvas = async (): Promise<HTMLCanvasElement | null> => {
    const el = cardRef.current;
    if (!el) return null;
    // Dynamic import keeps html2canvas (~50KB gz) out of the main bundle.
    const html2canvas = (await import("html2canvas")).default;
    return html2canvas(el, {
      backgroundColor: null,
      scale: 2, // retina-quality for sharing
      useCORS: true,
      logging: false,
    });
  };

  const onDownload = async () => {
    setRendering(true);
    try {
      const canvas = await toCanvas();
      if (!canvas) {
        toast.error("找不到渲染节点");
        return;
      }
      const link = document.createElement("a");
      const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      link.download = `${APP_NAME.toLowerCase()}-${ts}.png`;
      link.href = canvas.toDataURL("image/png");
      link.click();
      toast.success("已下载图片");
    } catch (e) {
      toast.error((e as Error).message || "导出失败");
    } finally {
      setRendering(false);
    }
  };

  const onCopy = async () => {
    setRendering(true);
    try {
      const canvas = await toCanvas();
      if (!canvas) {
        toast.error("找不到渲染节点");
        return;
      }
      // Clipboard image support: requires ClipboardItem + image/png.
      // Some browsers (older Firefox / Safari) don't support image clipboard;
      // we fall back to downloading.
      if (typeof ClipboardItem === "undefined" || !navigator.clipboard.write) {
        toast.warning("当前浏览器不支持复制图片，已切换为下载");
        await onDownload();
        return;
      }
      canvas.toBlob(async (blob) => {
        if (!blob) {
          toast.error("生成失败");
          setRendering(false);
          return;
        }
        try {
          await navigator.clipboard.write([
            new ClipboardItem({ "image/png": blob }),
          ]);
          toast.success("图片已复制到剪贴板");
        } catch (e) {
          toast.error("复制失败：" + (e as Error).message);
        } finally {
          setRendering(false);
        }
      }, "image/png");
    } catch (e) {
      toast.error((e as Error).message || "复制失败");
      setRendering(false);
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      onClick={() => !rendering && onClose()}
    >
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative flex h-full max-h-[90vh] w-full max-w-2xl flex-col rounded-2xl border bg-bg shadow-lift"
      >
        <header className="flex h-12 shrink-0 items-center justify-between border-b px-5">
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <ImageIcon className="h-4 w-4 text-accent" />
            分享卡片
          </h2>
          <button
            onClick={onClose}
            disabled={rendering}
            className="rounded-md p-1 text-muted hover:bg-surface hover:text-fg"
            aria-label="关闭"
            type="button"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        {/* Preview scroll area */}
        <div className="flex-1 overflow-y-auto bg-surface/40 p-6">
          {/* The actual share-card DOM — html2canvas snapshots this node. */}
          <div
            ref={cardRef}
            className="mx-auto w-full max-w-[560px] overflow-hidden rounded-2xl border border-border bg-bg shadow-lift"
          >
            {/* Top brand bar */}
            <div className="flex items-center gap-2.5 bg-gradient-to-r from-accent via-info to-accent px-5 py-3.5 text-white">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-white/15 backdrop-blur">
                <Sparkles className="h-4 w-4" />
              </div>
              <div className="leading-tight">
                <div className="text-sm font-bold">{APP_NAME}</div>
                <div className="text-[10px] text-white/80">
                  你的私有知识库 · 一句话提问
                </div>
              </div>
            </div>

            {/* Question (if provided) */}
            {question && (
              <div className="border-b border-border bg-surface/50 px-5 py-3">
                <div className="text-[10px] font-medium uppercase tracking-wider text-muted">
                  问题
                </div>
                <div className="mt-1 text-sm text-fg">{question}</div>
              </div>
            )}

            {/* Markdown body */}
            <div className="px-5 py-5">
              <article className="prose-tg max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {markdown.replace(/\\n/g, "\n")}
                </ReactMarkdown>
              </article>
            </div>

            {/* Footer watermark */}
            <div className="flex items-center justify-between border-t border-border bg-surface/40 px-5 py-3 text-[10px] text-muted">
              <span>
                由 <b className="text-fg">{APP_NAME}</b> 生成 · 上传文档秒级问答
              </span>
              <span>{new Date().toLocaleDateString("zh-CN")}</span>
            </div>
          </div>
        </div>

        {/* Actions */}
        <footer className="flex shrink-0 items-center justify-between gap-2 border-t bg-bg px-5 py-3">
          <p className="text-xs text-muted">
            高清 PNG · 适合朋友圈 / 群聊 / Slack 分享
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onCopy}
              disabled={rendering}
              className="btn btn-ghost btn-sm"
            >
              {rendering ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Copy className="h-3.5 w-3.5" />
              )}
              复制到剪贴板
            </button>
            <button
              type="button"
              onClick={onDownload}
              disabled={rendering}
              className="btn btn-primary btn-sm"
            >
              {rendering ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Download className="h-3.5 w-3.5" />
              )}
              下载图片
            </button>
          </div>
        </footer>
      </div>
    </div>,
    document.body
  );
}
