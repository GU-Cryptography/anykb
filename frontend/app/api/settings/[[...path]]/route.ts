import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

/**
 * Catch-all proxy for /api/settings/*  →  backend /api/settings/*
 *
 * v2-M1: forwards GET/POST/PUT/DELETE for the user settings endpoints.
 */
async function proxy(req: NextRequest, path: string[]): Promise<Response> {
  const sub = path.join("/");
  const search = req.nextUrl.search ?? "";
  const target = `${BACKEND_URL}/api/settings${sub ? "/" + sub : ""}${search}`;

  const headers = new Headers();
  const ct = req.headers.get("content-type");
  if (ct) headers.set("Content-Type", ct);
  const auth = req.headers.get("authorization");
  if (auth) headers.set("Authorization", auth);

  let body: ArrayBuffer | undefined;
  if (req.method !== "GET" && req.method !== "HEAD" && req.method !== "DELETE") {
    body = await req.arrayBuffer();
  }

  const upstream = await fetch(target, { method: req.method, headers, body });

  const respHeaders = new Headers();
  const upCT = upstream.headers.get("content-type");
  if (upCT) respHeaders.set("Content-Type", upCT);
  return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
}

type Ctx = { params: Promise<{ path?: string[] }> };

export async function GET(req: NextRequest, ctx: Ctx) {
  const { path = [] } = await ctx.params;
  return proxy(req, path);
}
export async function POST(req: NextRequest, ctx: Ctx) {
  const { path = [] } = await ctx.params;
  return proxy(req, path);
}
export async function PUT(req: NextRequest, ctx: Ctx) {
  const { path = [] } = await ctx.params;
  return proxy(req, path);
}
export async function DELETE(req: NextRequest, ctx: Ctx) {
  const { path = [] } = await ctx.params;
  return proxy(req, path);
}
