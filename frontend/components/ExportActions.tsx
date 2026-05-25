"use client";

import { useState } from "react";
import { Copy, FileText, Image as ImageIcon } from "lucide-react";
import { toast } from "sonner";

import ShareCardDialog from "@/components/ShareCardDialog";

type Props = {
  markdown: string;
  cost?: number | null;
  /** Optional — user question that triggered this assistant answer.
   *  Rendered as the share-card sub-heading when present. */
  question?: string;
};

export default function ExportActions({ markdown, cost, question }: Props) {
  const [shareOpen, setShareOpen] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(markdown);
      toast.success("已复制 Markdown 到剪贴板");
    } catch {
      toast.error("复制失败");
    }
  };

  const downloadPdf = async () => {
    // Dynamic import to keep initial bundle small.
    const html2pdf = (await import("html2pdf.js")).default;
    const el = document.getElementById("report-output");
    if (!el) {
      toast.error("找不到报告内容");
      return;
    }
    html2pdf().set({ filename: "anykb-report.pdf", margin: 10 }).from(el).save();
  };

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <button
          onClick={copy}
          className="inline-flex items-center gap-1 rounded-full border px-3 py-1.5 transition hover:bg-surface-2"
          type="button"
        >
          <Copy className="h-3.5 w-3.5" /> 复制
        </button>
        <button
          onClick={downloadPdf}
          className="inline-flex items-center gap-1 rounded-full border px-3 py-1.5 transition hover:bg-surface-2"
          type="button"
        >
          <FileText className="h-3.5 w-3.5" /> 导出 PDF
        </button>
        <button
          onClick={() => setShareOpen(true)}
          className="inline-flex items-center gap-1 rounded-full border px-3 py-1.5 transition hover:bg-surface-2"
          type="button"
        >
          <ImageIcon className="h-3.5 w-3.5" /> 图文分享
        </button>
        {cost != null && (
          <span className="ml-auto text-xs text-muted">
            本次成本 ≈ ${cost.toFixed(4)}
          </span>
        )}
      </div>

      <ShareCardDialog
        open={shareOpen}
        onClose={() => setShareOpen(false)}
        markdown={markdown}
        question={question}
      />
    </>
  );
}
