// Thin fetch wrapper: adds the bearer token, turns FastAPI errors into readable messages.

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body?: unknown,
  ) {
    super(message);
  }
}

// Per-tab session (sessionStorage), so two tabs can be two different demo users side by side.
const TOKEN_KEY = "eventai.session";
let memoryToken: string | null = null;

export function setToken(token: string | null) {
  memoryToken = token;
}

export function readToken(): string | null {
  if (memoryToken) return memoryToken;
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    return raw ? (JSON.parse(raw).access_token as string) : null;
  } catch {
    return null;
  }
}

function detailMessage(status: number, body: unknown): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d
      .map((e: { loc?: unknown[]; msg?: string }) =>
        [e.loc?.filter((x) => x !== "body").join("."), e.msg].filter(Boolean).join(": "),
      )
      .join("; ");
  }
  if (status === 429) return "Rate limit reached. Wait a moment and try again.";
  return `Request failed (${status})`;
}

export async function api<T>(
  path: string,
  init: { method?: string; body?: unknown; token?: string | null } = {},
): Promise<T> {
  const token = init.token === undefined ? readToken() : init.token;
  const res = await fetch(`/api${path}`, {
    method: init.method ?? (init.body === undefined ? "GET" : "POST"),
    headers: {
      ...(init.body !== undefined ? { "content-type": "application/json" } : {}),
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) throw new ApiError(res.status, detailMessage(res.status, body), body);
  return body as T;
}

export const SESSION_KEY = TOKEN_KEY;
