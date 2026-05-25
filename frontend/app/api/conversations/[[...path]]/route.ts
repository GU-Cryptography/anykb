import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

/**
 * Catch-all proxy for /api/conversations/*  →  backend /api/conversations/*
 *
 * Same shape as the kbs proxy (req.arrayBuffer forwarding lets future
 * multipart attachments survive intact). Supports GET / POST / PATCH / DELETE.
 */
async function proxy(req: NextRequest, path: string[]): Promise<Response> {
  const sub = path.join("/");
  const search = req.nextUrl.search ?? "";
  const target = `${BACKEND_URL}/api/conversations${sub ? "/" + sub : ""}${search}`;

  const headers = new Headers();
  const ct = req.headers.get("content-type");
  if (ct) headers.set("Content-Type", ct);
  const auth = req.headers.get("authorization");
  if (auth) headers.set("Authorization", auth);

  let body: ArrayBuffer | undefined;
  if (req.method !== "GET" && req.method !== "HEAD" && req.method !== "DELETE") {
    body = await req.arrayBuffer();
  }

  const upstream = await fetch(target, {
    method: req.method,
    headers,
    body,
  });

  const respHeaders = new Headers();
  const upCT = upstream.headers.get("content-type");
  if (upCT) respHeaders.set("Content-Type", upCT);
  const upCD = upstream.headers.get("content-disposition");
  if (upCD) respHeaders.set("Content-Disposition", upCD);
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
export async function DELETE(req: NextRequest, ctx: Ctx) {
  const { path = [] } = await ctx.params;
  return proxy(req, path);
}
export async function PATCH(req: NextRequest, ctx: Ctx) {
  const { path = [] } = await ctx.params;
  return proxy(req, path);
}
