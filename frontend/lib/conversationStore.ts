"use client";

import type { ToolEvent } from "@/components/ThinkingChain";

/**
 * Conversation type definitions + small pure helpers.
 *
 * v2-M3: storage moved server-side (see lib/conversations-api.ts). This file
 * keeps only the shape types and stateless utilities the UI still needs for
 * ephemeral streaming state (a temporary assistant message in React state
 * before SSE done fires and we persist).
 */

export type Message =
  | {
      id: string;
      role: "user";
      content: string;
      created_at: number;
    }
  | {
      id: string;
      role: "assistant";
      content: string;          // markdown report
      tools: ToolEvent[];        // tool call timeline
      streaming?: boolean;
      cost_usd?: number;
      error?: string;
      created_at: number;
    };

export type Conversation = {
  id: string;
  title: string;
  messages: Message[];
  /** KB this conversation is bound to. null = unbound mode (multi-tool agent). */
  kb_id?: string | null;
  /** v3-M6: per-conversation LLM model override. null = use user default. */
  llm_model?: string | null;
  /** v3-M4: ISO timestamp once finalized (long-term memory extracted); null = open. */
  finalized_at?: string | null;
  created_at: number;
  updated_at: number;
};

export function deriveTitle(msg: string): string {
  const cleaned = msg.trim().replace(/\s+/g, " ");
  return cleaned.length > 24 ? cleaned.slice(0, 24) + "…" : cleaned;
}

export function genMessageId(): string {
  return `msg_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
}
