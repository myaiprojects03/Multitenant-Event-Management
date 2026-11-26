// Same-origin proxy to the FastAPI backend, so the browser never needs CORS and the API
// address is a runtime setting (API_URL), not baked into the build.
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const API_URL = process.env.API_URL ?? "http://localhost:8000";
const PASS_REQUEST = ["authorization", "content-type", "accept", "x-platform-timestamp", "x-platform-signature"];
const PASS_RESPONSE = ["content-type", "retry-after", "x-request-id"];

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const url = `${API_URL}/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const h of PASS_REQUEST) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  const hasBody = !["GET", "HEAD"].includes(req.method);
  try {
    const res = await fetch(url, {
      method: req.method,
      headers,
      body: hasBody ? await req.arrayBuffer() : undefined,
      cache: "no-store",
    });
    const out = new Headers();
    for (const h of PASS_RESPONSE) {
      const v = res.headers.get(h);
      if (v) out.set(h, v);
    }
    return new Response(res.status === 204 ? null : await res.arrayBuffer(), {
      status: res.status,
      headers: out,
    });
  } catch {
    return Response.json({ detail: "The API is not reachable. Is the backend running?" }, { status: 502 });
  }
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
