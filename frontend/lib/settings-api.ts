"use client";

import { authFetch } from "./auth";

/**
 * Settings API client (v2-M1).
 *
 * Mirrors backend/src/settings_user/routes.py. Throws `SettingsApiError` so the
 * page can branch on dim-conflict (HTTP 409 with structured detail).
 */

export type LLMProvider = "anthropic" | "openai-compat";
export type EmbeddingProvider = "openai-compat" | "ollama";
export type RerankerProvider = "siliconflow" | "cohere" | "openai-compat";

export type MyLLMSettings = {
  provider: LLMProvider | null;
  base_url: string | null;
  default_model: string | null;
  complex_model: string | null;
  has_key: boolean;
  configured: boolean;
};

export type MyEmbeddingSettings = {
  provider: EmbeddingProvider | null;
  base_url: string | null;
  model: string | null;
  dim: number | null;
  has_key: boolean;
  configured: boolean;
};

export type MyRerankerSettings = {
  /** v3-M4: per-user cross-encoder reranker (opt-in, default off). */
  provider: RerankerProvider | null;
  base_url: string | null;
  model: string | null;
  has_key: boolean;
  /** True when provider+base_url+model are all populated (api_key optional for self-hosted). */
  configured: boolean;
  /** Master toggle — both `configured` and `enabled` must be true for rerank to fire at chat time. */
  enabled: boolean;
};

export type MyKbOptions = {
  /** v2-M6: opt-in to mount web_search as a fallback tool when chatting against a user KB. */
  kb_web_search_enabled: boolean;
};

export type MySettings = {
  llm: MyLLMSettings;
  embedding: MyEmbeddingSettings;
  reranker: MyRerankerSettings;
  kb_options: MyKbOptions;
};

export type SaveLLMBody = {
  provider: LLMProvider;
  base_url: string;
  api_key: string;
  default_model: string;
  complex_model?: string;
};

export type SaveEmbeddingBody = {
  provider: EmbeddingProvider;
  base_url: string;
  api_key: string;
  model: string;
  dim: number;
};

export type SaveRerankerBody = {
  provider: RerankerProvider;
  base_url: string;
  /** Empty string = keep existing encrypted key (lets user toggle enable without re-entering). */
  api_key: string;
  model: string;
  enabled: boolean;
};

export type ProbeLLMBody = {
  provider: LLMProvider;
  base_url: string;
  api_key: string;
};

export type ProbeEmbeddingBody = {
  provider: EmbeddingProvider;
  base_url: string;
  api_key: string;
  model?: string;
};

export type ProbeRerankerBody = {
  provider: RerankerProvider;
  base_url: string;
  api_key: string;
};

export type DimConflictDetail = {
  code: "embedding_dim_conflict";
  message: string;
  new_dim: number;
  affected_kbs: Array<{ id: string; name: string; vector_size: number }>;
};

export class SettingsApiError extends Error {
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
      message =
        typeof detail === "string"
          ? detail
          : typeof (detail as { message?: string })?.message === "string"
            ? (detail as { message: string }).message
            : JSON.stringify(detail);
    } catch {
      /* keep default */
    }
    throw new SettingsApiError(r.status, detail, message);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------
export async function getMySettings(): Promise<MySettings> {
  return unwrap(await authFetch("/api/settings/me"));
}

export async function saveLLMSettings(body: SaveLLMBody): Promise<MySettings> {
  return unwrap(
    await authFetch("/api/settings/llm", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

export async function clearLLMSettings(): Promise<void> {
  await unwrap(await authFetch("/api/settings/llm", { method: "DELETE" }));
}

export async function saveEmbeddingSettings(
  body: SaveEmbeddingBody
): Promise<MySettings> {
  return unwrap(
    await authFetch("/api/settings/embedding", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

export async function clearEmbeddingSettings(): Promise<void> {
  await unwrap(await authFetch("/api/settings/embedding", { method: "DELETE" }));
}

export async function probeLLM(body: ProbeLLMBody): Promise<{ models: string[] }> {
  return unwrap(
    await authFetch("/api/settings/probe/llm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

export async function probeEmbedding(
  body: ProbeEmbeddingBody
): Promise<{ models: string[]; dim: number | null }> {
  return unwrap(
    await authFetch("/api/settings/probe/embedding", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

// v2-M6: KB-mode toggles (currently just web_search opt-in).
export async function saveKbOptions(body: MyKbOptions): Promise<MySettings> {
  return unwrap(
    await authFetch("/api/settings/kb-options", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

// v3-M4: cross-encoder reranker (opt-in, default off).
export async function saveRerankerSettings(body: SaveRerankerBody): Promise<MySettings> {
  return unwrap(
    await authFetch("/api/settings/reranker", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

export async function clearRerankerSettings(): Promise<void> {
  await unwrap(await authFetch("/api/settings/reranker", { method: "DELETE" }));
}

export async function probeReranker(
  body: ProbeRerankerBody
): Promise<{ models: string[] }> {
  return unwrap(
    await authFetch("/api/settings/probe/reranker", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}
