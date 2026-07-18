"use client";

import { authFetch } from "./auth";
import type { ToolEvent } from "@/components/ThinkingChain";

/**
 * Conversations API client (v2-M3).
 *
 * Replaces the v1 localStorage-only store. All endpoints proxied through
 * /api/conversations/* (Next route hops to backend with Bearer attached).
 *
 * Frontend orchestration (no server-side chat persistence):
 *   1. user types        → appendMessage(role: "user")
 *   2. SSE chat fires    → existing sseClient.connectChat
 *   3. SSE done/error    → appendMessage(role: "assistant", tools, cost_usd, error?)
 *
 * Keep types in sync with backend/src/conversations/models.py.
 */

export type ConversationSummary = {
  id: string;
  title: string;
  kb_id: string | null;
  /** v3-M6: per-conversation LLM model override. null = use user default. */
  llm_model: string | null;
  message_count: number;
  created_at: string | null;
  updated_at: string | null;
  /** v3-M4: set once the conversation is finalized (long-term memory extracted).
   *  null = still open. Lets the sidebar hide/disable the finalize action. */
  finalized_at: string | null;
};

export type MessagePayload = {
  id: string;
  role: "user" | "assistant";
  content: string;
  tools: ToolEvent[] | null;
  cost_usd: number | null;
  error: string | null;
  created_at: string | null;
};

export type ConversationDetail = ConversationSummary & {
  messages: MessagePayload[];
};

/** Structured error mirror of KbApiError. */
export class ConversationApiError extends Error {
  status: number;
  detail: unknown;
  code?: string;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
    if (detail && typeof detail === "object") {
      const d = detail as { code?: string };
      this.code = d.code;
    }
  }
}

async function unwrap<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let detail: unknown = null;
    let message = `HTTP ${r.status}`;
    try {
      const j = await r.json();
      detail = j.detail ?? j;
      if (typeof detail === "string") {
        message = detail;
      } else if (detail && typeof detail === "object") {
        const d = detail as { message?: string };
        message = typeof d.message === "string" ? d.message : JSON.stringify(detail);
      }
    } catch {
      /* keep default */
    }
    throw new ConversationApiError(r.status, detail, message);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

// ---------------------------------------------------------------------------
// CRUD
// ---------------------------------------------------------------------------
export async function listConversations(): Promise<ConversationSummary[]> {
  return unwrap(await authFetch("/api/conversations"));
}

export async function getConversation(id: string): Promise<ConversationDetail> {
  return unwrap(await authFetch(`/api/conversations/${id}`));
}

export async function createConversation(
  opts: { kb_id?: string | null; title?: string } = {}
): Promise<ConversationDetail> {
  return unwrap(
    await authFetch("/api/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kb_id: opts.kb_id ?? null, title: opts.title }),
    })
  );
}

export async function patchConversation(
  id: string,
  patch: { title?: string; kb_id?: string | null; llm_model?: string | null }
): Promise<ConversationSummary> {
  return unwrap(
    await authFetch(`/api/conversations/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })
  );
}

export async function deleteConversation(id: string): Promise<void> {
  await unwrap(await authFetch(`/api/conversations/${id}`, { method: "DELETE" }));
}

// ---------------------------------------------------------------------------
// v3-M4: finalize → long-term memory extraction (PRD §8)
// ---------------------------------------------------------------------------
export type FinalizeResult = {
  /** Number of long-term memories extracted + stored from this conversation. */
  memory_extracted: number;
  /** True iff at least one memory was stored (the L1 profile may have shifted). */
  profile_updated: boolean;
  /** True when the conversation was already finalized → this call was a no-op. */
  already_finalized: boolean;
};

/**
 * Mark a conversation finished and extract long-term memories from it.
 *
 * Idempotent server-side: a second call returns already_finalized=true with
 * memory_extracted=0. The backend awaits extraction (a user-initiated action),
 * so the returned count is the real number stored.
 */
export async function finalizeConversation(id: string): Promise<FinalizeResult> {
  return unwrap(
    await authFetch(`/api/conversations/${id}/finalize`, { method: "POST" })
  );
}

// ---------------------------------------------------------------------------
// v3-M5: bulk delete + export
// ---------------------------------------------------------------------------
export async function deleteAllConversations(): Promise<void> {
  await unwrap(await authFetch("/api/conversations", { method: "DELETE" }));
}

/** Trigger a browser download of all conversations as JSON. */
export async function exportConversations(): Promise<void> {
  const r = await authFetch("/api/conversations/export");
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
    throw new ConversationApiError(r.status, detail, `export failed: ${r.status}`);
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "anykb-export.json";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------
export async function appendUserMessage(
  convId: string,
  content: string
): Promise<MessagePayload> {
  return unwrap(
    await authFetch(`/api/conversations/${convId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "user", content }),
    })
  );
}

export async function appendAssistantMessage(
  convId: string,
  payload: {
    content: string;
    tools?: ToolEvent[];
    cost_usd?: number;
    error?: string;
  }
): Promise<MessagePayload> {
  return unwrap(
    await authFetch(`/api/conversations/${convId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        role: "assistant",
        content: payload.content,
        tools: payload.tools && payload.tools.length > 0 ? payload.tools : undefined,
        cost_usd: payload.cost_usd,
        error: payload.error,
      }),
    })
  );
}

// ---------------------------------------------------------------------------
// Bulk import (v1 localStorage → server, one-shot per browser)
// ---------------------------------------------------------------------------
type LocalMessage =
  | { role: "user"; content: string; created_at?: number }
  | {
      role: "assistant";
      content: string;
      tools?: ToolEvent[];
      cost_usd?: number;
      error?: string;
      created_at?: number;
    };

type LocalConversation = {
  id?: string;
  title?: string;
  kb_id?: string | null;
  created_at?: number;
  updated_at?: number;
  messages: LocalMessage[];
};

/**
 * Push any leftover localStorage conversations to the server, exactly once.
 * Idempotent via `anykb:migrated:{userId}` flag.
 *
 * Returns the imported count (0 if nothing to migrate or already migrated).
 */
export async function migrateFromLocalStorage(userId: string): Promise<number> {
  if (typeof window === "undefined") return 0;
  const ls = window.localStorage;
  const migratedFlag = `anykb:migrated:${userId}`;
  if (ls.getItem(migratedFlag) === "true") return 0;

  const oldConvKey = `anykb:conversations:${userId}`;
  const oldCurrKey = `anykb:current_conversation_id:${userId}`;
  const raw = ls.getItem(oldConvKey);
  if (!raw) {
    ls.setItem(migratedFlag, "true");
    return 0;
  }

  let list: LocalConversation[] = [];
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) list = parsed as LocalConversation[];
  } catch {
    // Garbage data — clear it and move on, no point retrying.
    ls.removeItem(oldConvKey);
    ls.removeItem(oldCurrKey);
    ls.setItem(migratedFlag, "true");
    return 0;
  }

  if (list.length === 0) {
    ls.removeItem(oldConvKey);
    ls.removeItem(oldCurrKey);
    ls.setItem(migratedFlag, "true");
    return 0;
  }

  const body = {
    conversations: list.map((c) => ({
      title: c.title ?? "新对话",
      kb_id: c.kb_id ?? null,
      created_at: c.created_at,
      updated_at: c.updated_at,
      messages: (c.messages ?? []).map((m) => {
        if (m.role === "user") {
          return { role: "user", content: m.content, created_at: m.created_at };
        }
        return {
          role: "assistant",
          content: m.content,
          tools: m.tools,
          cost_usd: m.cost_usd,
          error: m.error,
          created_at: m.created_at,
        };
      }),
    })),
  };

  const result = await unwrap<{ imported: number }>(
    await authFetch("/api/conversations/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );

  // Only clean up on success — leaves data intact for retry on failure.
  ls.removeItem(oldConvKey);
  ls.removeItem(oldCurrKey);
  ls.setItem(migratedFlag, "true");
  return result.imported ?? 0;
}
