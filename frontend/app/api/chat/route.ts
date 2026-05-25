import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

const SSE_HEADERS = {
  "Content-Type": "text/event-stream",
  "Cache-Control": "no-cache, no-transform",
  Connection: "keep-alive",
};

export async function POST(req: NextRequest) {
  const body = await req.text();

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  const auth = req.headers.get("authorization");
  if (auth) headers["Authorization"] = auth;

  const upstream = await fetch(`${BACKEND_URL}/api/chat`, {
    method: "POST",
    headers,
    body,
  });

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text().catch(() => "");
    return new Response(text || `backend error ${upstream.status}`, {
      status: upstream.status,
    });
  }

  return new Response(upstream.body, { headers: SSE_HEADERS });
}

// Deprecated single-turn GET — kept for backward compatibility / smoke tests.
export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams.get("q");
  if (!q) return new Response("missing q", { status: 400 });

  const upstream = await fetch(
    `${BACKEND_URL}/api/chat?q=${encodeURIComponent(q)}`,
    { headers: { Accept: "text/event-stream" } }
  );

  if (!upstream.ok || !upstream.body) {
    return new Response(`backend error ${upstream.status}`, {
      status: upstream.status,
    });
  }

  return new Response(upstream.body, { headers: SSE_HEADERS });
}
