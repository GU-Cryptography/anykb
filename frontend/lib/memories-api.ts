"use client";

import { authFetch } from "./auth";

/**
 * Long-term memory API client (v3-M4, PRD §8).
 *
 * Mirrors backend/src/conversations/memory_routes.py. All calls go through
 * authFetch (Bearer token) → /api/memories/* (Next proxy hops to the backend).
 * Every route is owner-scoped server-side; a foreign/missing id 404s.
 *
 * Keep the MemoryType union + DTO shape in sync with UserMemory.to_public_dict().
 */

export type MemoryType =
  | "profile"
  | "preference"
  | "fact"
  | "task"
  | "skill"
  | "explicit";

export type Memory = {
  id: string;
  type: MemoryType;
  content: string;
  importance: number;
  source_conversation_id: string | null;
  created_at: string | null;
};

export type MemoryListResponse = {
  total: number;
  limit: number;
  offset: number;
  memories: Memory[];
};

/** Structured error mirror of ConversationApiError. */
export class MemoryApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
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
    throw new MemoryApiError(r.status, detail, message);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

// ---------------------------------------------------------------------------
// CRUD
// ---------------------------------------------------------------------------
export async function listMemories(
  opts: { type?: MemoryType | null; limit?: number; offset?: number } = {}
): Promise<MemoryListResponse> {
  const params = new URLSearchParams();
  if (opts.type) params.set("type", opts.type);
  params.set("limit", String(opts.limit ?? 50));
  params.set("offset", String(opts.offset ?? 0));
  return unwrap(await authFetch(`/api/memories?${params.toString()}`));
}

export async function updateMemory(
  id: string,
  patch: { content?: string; importance?: number }
): Promise<Memory> {
  return unwrap(
    await authFetch(`/api/memories/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })
  );
}

export async function deleteMemory(id: string): Promise<void> {
  await unwrap(await authFetch(`/api/memories/${id}`, { method: "DELETE" }));
}
