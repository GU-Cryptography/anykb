"use client";

import { User, Bot } from "lucide-react";
import ThinkingChain, { type ToolEvent } from "@/components/ThinkingChain";
import ReportView from "@/components/ReportView";
import ExportActions from "@/components/ExportActions";
import type { Message } from "@/lib/conversationStore";

export default function MessageBubble({
  message,
  prevUserMessage,
}: {
  message: Message;
  /** v3.1: pass-through to ExportActions → ShareCardDialog renders "Q: ..." sub-heading. */
  prevUserMessage?: string;
}) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="flex max-w-[85%] items-start gap-2">
          <div className="rounded-2xl rounded-tr-sm bg-accent px-4 py-2.5 text-white whitespace-pre-wrap break-words">
            {message.content}
          </div>
          <div className="mt-1 flex h-7 w-7 flex-none items-center justify-center rounded-full bg-accent/15 text-accent">
            <User className="h-4 w-4" />
          </div>
        </div>
      </div>
    );
  }

  // assistant
  const hasContent = message.content && message.content.length > 0;
  const hasTools = message.tools && message.tools.length > 0;
  const streaming = !!message.streaming;

  const showInitialThinking = streaming && !hasTools && !hasContent && !message.error;
  const showWritingHint =
    streaming &&
    hasTools &&
    !hasContent &&
    !message.error &&
    message.tools.every((t) => t.status !== "running");

  return (
    <div className="flex justify-start">
      <div className="flex max-w-full items-start gap-2 w-full">
        <div className="mt-1 flex h-7 w-7 flex-none items-center justify-center rounded-full bg-fg/10 text-fg/80">
          <Bot className="h-4 w-4" />
        </div>
        <div className="flex-1 space-y-3 min-w-0">
          {showInitialThinking && <ThinkingPlaceholder label="正在思考" />}

          {hasTools && <ThinkingChain events={message.tools} />}

          {showWritingHint && <ThinkingPlaceholder label="正在撰写报告" />}

          {message.error && (
            <div className="rounded-md border border-red-300/40 bg-red-50/60 p-3 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300">
              ⚠️ {message.error}
            </div>
          )}

          {hasContent && (
            <ReportView markdown={message.content} streaming={streaming} />
          )}

          {!hasContent && !streaming && !message.error && (
            <div className="text-sm text-muted">（无内容）</div>
          )}

          {hasContent && !streaming && (
            <ExportActions
              markdown={message.content}
              cost={message.cost_usd ?? null}
              question={prevUserMessage}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function ThinkingPlaceholder({ label }: { label: string }) {
  return (
    <div className="inline-flex items-center gap-2 rounded-xl border border-fg/10 bg-fg/[0.02] px-3 py-2 text-sm text-muted">
      <span className="relative inline-flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
      </span>
      <span>{label}</span>
      <span className="inline-flex gap-0.5">
        <span className="h-1 w-1 animate-bounce rounded-full bg-muted [animation-delay:-0.3s]" />
        <span className="h-1 w-1 animate-bounce rounded-full bg-muted [animation-delay:-0.15s]" />
        <span className="h-1 w-1 animate-bounce rounded-full bg-muted" />
      </span>
    </div>
  );
}
