"use client";

import { Send, Square } from "lucide-react";
import { useEffect, useRef, useState, KeyboardEvent } from "react";

type Props = {
  onSend: (q: string) => void;
  onStop?: () => void;
  busy?: boolean;
  placeholder?: string;
};

const DEFAULT_PLACEHOLDER =
  "问点什么... 例如：总结一下这份知识库的主要内容";

export default function ChatBox({ onSend, onStop, busy, placeholder }: Props) {
  const [value, setValue] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [value]);

  // Auto-focus
  useEffect(() => {
    if (!busy) taRef.current?.focus();
  }, [busy]);

  const submit = () => {
    const q = value.trim();
    if (!q || busy) return;
    onSend(q);
    setValue("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="rounded-2xl border bg-bg shadow-soft transition focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/20">
      <textarea
        ref={taRef}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder ?? DEFAULT_PLACEHOLDER}
        rows={1}
        disabled={busy && !onStop}
        className="block w-full resize-none bg-transparent px-4 pt-3.5 pb-1 text-[15px] outline-none placeholder:text-muted disabled:opacity-50"
        style={{ maxHeight: 200 }}
      />
      <div className="flex items-center justify-between px-3 pb-2 pt-1 text-xs text-muted">
        <span className="hidden sm:inline">Enter 发送 · Shift+Enter 换行</span>
        <span className="sm:hidden">回车发送</span>
        {busy && onStop ? (
          <button
            onClick={onStop}
            className="inline-flex items-center gap-1 rounded-full border px-3 py-1.5 transition hover:bg-surface-2"
            type="button"
          >
            <Square className="h-3 w-3 fill-current" />
            停止
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!value.trim()}
            className="inline-flex items-center gap-1 rounded-full bg-accent px-3 py-1.5 text-white transition hover:bg-accent/90 disabled:opacity-40"
            type="button"
          >
            <Send className="h-3.5 w-3.5" />
            发送
          </button>
        )}
      </div>
    </div>
  );
}
