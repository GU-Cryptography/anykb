import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

/**
 * Catch-all proxy for /api/admin/*  →  backend /api/admin/*
 * Forwards method, body, and Authorization header transparently.
 *
 * Uses an optional catch-all ([[...path]]) so the bare /api/admin root proxies
 * too — same convention as /api/kbs and /api/conversations.
 */
async function proxy(req: NextRequest, path: string[] | undefined): Promise<Response> {
  const suffix = (path ?? []).join("/");
  const target = `${BACKEND_URL}/api/admin${suffix ? `/${suffix}` : ""}${req.nextUrl.search}`;

  const headers = new Headers();
  headers.set("Content-Type", req.headers.get("content-type") ?? "application/json");
  const auth = req.headers.get("authorization");
  if (auth) headers.set("Authorization", auth);

  const body = req.method === "GET" || req.method === "HEAD" ? undefined : await req.text();

  const upstream = await fetch(target, {
    method: req.method,
    headers,
    body,
  });

  const respHeaders = new Headers();
  respHeaders.set("Content-Type", upstream.headers.get("content-type") ?? "application/json");
  return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path?: string[] }> }) {
  const { path } = await ctx.params;
  return proxy(req, path);
}

export async function POST(req: NextRequest, ctx: { params: Promise<{ path?: string[] }> }) {
  const { path } = await ctx.params;
  return proxy(req, path);
}

export async function PATCH(req: NextRequest, ctx: { params: Promise<{ path?: string[] }> }) {
  const { path } = await ctx.params;
  return proxy(req, path);
}

export async function DELETE(req: NextRequest, ctx: { params: Promise<{ path?: string[] }> }) {
  const { path } = await ctx.params;
  return proxy(req, path);
}
