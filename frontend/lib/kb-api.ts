"use client";

import { authFetch } from "./auth";

/**
 * KB / Document API client.
 *
 * All calls go through authFetch which auto-attaches the Bearer token.
 * Server-side type is the source of truth; these mirrors live here so the UI
 * gets static checking. Keep them in sync with backend/src/kb/models.py.
 */

export type DocStatus = "pending" | "ingesting" | "done" | "failed";
export type SourceType = "file" | "url";
/** v2-M9: caller's effective role for a KB. system KB is "viewer" for everyone. */
export type KbRole = "owner" | "editor" | "viewer";
export type MemberRole = "editor" | "viewer";

export type KB = {
  id: string;
  name: string;
  description: string;
  embedding_model: string;
  vector_size: number;
  chunks_count: number;
  documents_count: number;
  is_system: boolean;
  /** v3-M3: owner toggle. When true, KB search returns at most 1 chunk per
   *  document via Milvus group_by_field. */
  grouping_enabled: boolean;
  created_at: string | null;
  /** v2-M9: present when returned by list_kbs / get_kb. Absent on POST create. */
  my_role?: KbRole;
};

export type Document = {
  id: string;
  kb_id: string;
  filename: string;
  mime: string;
  size_bytes: number;
  source_type: SourceType;
  source_url: string;
  status: DocStatus;
  chunks_count: number;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type KBDetail = KB & { documents: Document[] };

/** v2-M9: response shape for GET /kbs/{id}/members */
export type KbMemberListResponse = {
  owner: {
    user_id: string;
    email: string;
    display_name: string | null;
  } | null;
  members: {
    user_id: string;
    email: string;
    display_name: string | null;
    role: MemberRole;
    invited_by_email: string | null;
    created_at: string | null;
  }[];
};

/** v2-M9: share-link invitation row. id IS the URL token. */
export type KbInvitation = {
  id: string;
  kb_id: string;
  role: MemberRole;
  created_by: string;
  expires_at: string | null;
  max_uses: number | null;
  uses_count: number;
  revoked: boolean;
  created_at: string | null;
};

/** v2-M9: GET /api/invitations/{token} preview shape (before accept). */
export type InvitationPreview = {
  kb_id: string;
  kb_name: string;
  role: MemberRole;
  expires_at: string | null;
  max_uses: number | null;
  uses_count: number;
};

/** Structured error from KB endpoints. v2-M2 BYOK gate surfaces `detail.code`
 *  (e.g. "embedding_not_configured") so the page can route to /settings. */
export class KbApiError extends Error {
  status: number;
  detail: unknown;
  code?: string;
  settings_url?: string;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
    if (detail && typeof detail === "object") {
      const d = detail as { code?: string; settings_url?: string };
      this.code = d.code;
      this.settings_url = d.settings_url;
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
    throw new KbApiError(r.status, detail, message);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

// ---------------------------------------------------------------------------
// KB CRUD
// ---------------------------------------------------------------------------
export async function listKbs(): Promise<KB[]> {
  return unwrap(await authFetch("/api/kbs"));
}

export async function getKb(id: string): Promise<KBDetail> {
  return unwrap(await authFetch(`/api/kbs/${id}`));
}

/** v3-M7: optional per-KB embedding + reranker override at creation time. */
export type CreateKbBody = {
  name: string;
  description?: string;
  embedding_provider?: "openai-compat" | "ollama" | null;
  embedding_base_url?: string | null;
  embedding_api_key?: string;
  embedding_model?: string | null;
  embedding_dim?: number | null;
  reranker_provider?: "siliconflow" | "cohere" | "openai-compat" | null;
  reranker_base_url?: string | null;
  reranker_api_key?: string;
  reranker_model?: string | null;
  reranker_enabled?: boolean;
};

export async function createKb(
  nameOrBody: string | CreateKbBody,
  description = ""
): Promise<KB> {
  // Back-compat overload: createKb("name", "desc") still works.
  const body: CreateKbBody =
    typeof nameOrBody === "string"
      ? { name: nameOrBody, description }
      : nameOrBody;
  return unwrap(
    await authFetch("/api/kbs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

export async function deleteKb(id: string): Promise<void> {
  await unwrap(await authFetch(`/api/kbs/${id}`, { method: "DELETE" }));
}

/** v3-M3: owner-only KB settings PATCH. Currently only supports
 *  `grouping_enabled` toggle. Returns the updated KB. */
export async function patchKb(
  id: string,
  body: { grouping_enabled?: boolean }
): Promise<KB> {
  return unwrap(
    await authFetch(`/api/kbs/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

/** v3-M3: owner-only KB rebuild. Drops the vector collection and re-ingests
 *  every document — used to upgrade a pre-v3-M3 dense-only collection to
 *  the hybrid (dense + BM25) schema. Returns count of docs being re-ingested.
 *  During the rebuild window chat against this KB sees empty hits. */
export async function rebuildKb(
  id: string
): Promise<{ rebuilding: boolean; doc_count: number; collection: string }> {
  return unwrap(
    await authFetch(`/api/kbs/${id}/rebuild`, { method: "POST" })
  );
}

// ---------------------------------------------------------------------------
// Document operations
// ---------------------------------------------------------------------------
export async function listDocuments(kbId: string): Promise<Document[]> {
  return unwrap(await authFetch(`/api/kbs/${kbId}/documents`));
}

export async function uploadFile(kbId: string, file: File): Promise<Document> {
  const fd = new FormData();
  fd.append("file", file);
  return unwrap(
    await authFetch(`/api/kbs/${kbId}/documents`, {
      method: "POST",
      body: fd,
    })
  );
}

export async function uploadUrl(kbId: string, url: string): Promise<Document> {
  const fd = new FormData();
  fd.append("url", url);
  return unwrap(
    await authFetch(`/api/kbs/${kbId}/documents`, {
      method: "POST",
      body: fd,
    })
  );
}

export async function deleteDocument(kbId: string, docId: string): Promise<void> {
  await unwrap(
    await authFetch(`/api/kbs/${kbId}/documents/${docId}`, { method: "DELETE" })
  );
}

// ---------------------------------------------------------------------------
// v2-M9: Members management
// ---------------------------------------------------------------------------
export async function listMembers(kbId: string): Promise<KbMemberListResponse> {
  return unwrap(await authFetch(`/api/kbs/${kbId}/members`));
}

export async function inviteMember(
  kbId: string,
  email: string,
  role: MemberRole
): Promise<KbMemberListResponse["members"][number]> {
  return unwrap(
    await authFetch(`/api/kbs/${kbId}/members`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, role }),
    })
  );
}

export async function patchMember(
  kbId: string,
  userId: string,
  role: MemberRole
): Promise<void> {
  await unwrap(
    await authFetch(`/api/kbs/${kbId}/members/${userId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    })
  );
}

export async function removeMember(kbId: string, userId: string): Promise<void> {
  await unwrap(
    await authFetch(`/api/kbs/${kbId}/members/${userId}`, { method: "DELETE" })
  );
}

// ---------------------------------------------------------------------------
// v2-M9: Share-link invitations
// ---------------------------------------------------------------------------
export async function listInvitations(kbId: string): Promise<KbInvitation[]> {
  return unwrap(await authFetch(`/api/kbs/${kbId}/invitations`));
}

export async function createInvitation(
  kbId: string,
  body: { role: MemberRole; expires_at?: string | null; max_uses?: number | null }
): Promise<KbInvitation> {
  return unwrap(
    await authFetch(`/api/kbs/${kbId}/invitations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

export async function deleteInvitation(
  kbId: string,
  invitationId: string
): Promise<void> {
  await unwrap(
    await authFetch(`/api/kbs/${kbId}/invitations/${invitationId}`, {
      method: "DELETE",
    })
  );
}

/** Public preview before user clicks "accept". Returns 404 / 410 if invalid. */
export async function peekInvitation(token: string): Promise<InvitationPreview> {
  return unwrap(await authFetch(`/api/invitations/${token}`));
}

/** Accepts the invitation; idempotent. Returns the kb_id + role granted. */
export async function acceptInvitation(
  token: string
): Promise<{ kb_id: string; role: KbRole }> {
  return unwrap(
    await authFetch(`/api/invitations/${token}/accept`, { method: "POST" })
  );
}
