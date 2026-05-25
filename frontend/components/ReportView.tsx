"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function ReportView({ markdown, streaming }: { markdown: string; streaming: boolean }) {
  return (
    <article
      id="report-output"
      className="prose-tg rounded-2xl rounded-tl-sm border bg-surface px-4 py-3 shadow-soft"
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown.replace(/\\n/g, "\n")}</ReactMarkdown>
      {streaming && (
        <span className="ml-1 inline-block h-4 w-1.5 animate-pulse bg-accent align-middle" />
      )}
    </article>
  );
}
