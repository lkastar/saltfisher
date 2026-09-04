/** The only module that knows about HTTP, URLs, or the API token.
 *  No component calls fetch().
 */

const TOKEN_KEY = "sfd_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  // A plain field rather than a parameter property: the template enables
  // erasableSyntaxOnly, which rejects constructor-parameter declarations.
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** FastAPI puts the readable reason in `detail`, which is either a string or,
 *  for a 422, a list of per-field validation errors. Both must reach the user:
 *  a blank screen with a red border is the failure mode this replaces.
 */
async function readError(res: Response): Promise<string> {
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    // A gateway status with no body means the request never reached the app:
    // the dev proxy or the container returned it. "HTTP 502" tells the user
    // nothing they can act on; naming the cause does.
    if (res.status >= 502 && res.status <= 504) {
      return "后端无响应，确认服务是否在运行";
    }
    return "服务器未返回错误详情";
  }
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      const e = d as { loc?: unknown[]; msg?: string };
      const field = Array.isArray(e.loc) ? e.loc.slice(1).join(".") : "";
      return field ? `${field}: ${e.msg ?? ""}` : (e.msg ?? "");
    });
    const joined = parts.filter(Boolean).join("；");
    if (joined) return joined;
  }
  return `HTTP ${res.status}`;
}

async function send(
  path: string,
  init: RequestInit,
  token: string | null,
): Promise<unknown> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");

  const res = await fetch(path, { ...init, headers });
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  if (res.status === 204) return null;
  return res.json();
}

/** Every call from the app. A 401 here means the stored token is no longer
 *  accepted, so it is dropped and the page reloads into the login screen.
 *
 *  ponytail: a full reload rather than a router navigation, because it also
 *  discards the Query cache filled under the old token. A single-user panel
 *  hits this at most once per token rotation; a state machine would cost more
 *  than it saves.
 */
export async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  try {
    return (await send(path, init, getToken())) as T;
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      clearToken();
      window.location.reload();
    }
    throw err;
  }
}

/** Login's probe. Uses the candidate token explicitly and never clears or
 *  reloads, so a wrong token surfaces as an inline message instead of
 *  bouncing the user off the form they are still filling in.
 */
export async function verifyToken(candidate: string): Promise<void> {
  await send("/api/monitors", { method: "GET" }, candidate);
}
