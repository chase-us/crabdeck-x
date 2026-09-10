export const MAX_JSON_BYTES = 32 * 1024;

export type ActorClass = "authed" | "anon";

export type RouteClass =
  | "edge"
  | "ws"
  | "gw"
  | "vault"
  | "api"
  | "other";

export function classifyRoute(pathname: string): RouteClass {
  if (
    pathname === "/" ||
    pathname === "/health" ||
    pathname === "/ready"
  ) {
    return "edge";
  }
  if (pathname === "/ws" || pathname.startsWith("/ws/")) {
    return "ws";
  }
  if (pathname === "/gw" || pathname.startsWith("/gw/")) {
    return "gw";
  }
  if (pathname === "/vault" || pathname.startsWith("/vault/")) {
    return "vault";
  }
  if (pathname === "/api" || pathname.startsWith("/api/")) {
    return "api";
  }
  return "other";
}

export function stripPrefix(pathname: string, prefix: string): string {
  if (pathname === prefix) {
    return "/";
  }
  if (pathname.startsWith(`${prefix}/`)) {
    const rest = pathname.slice(prefix.length);
    return rest.length === 0 ? "/" : rest;
  }
  return pathname;
}

export function contentLengthExceeds(
  request: Request,
  maxBytes: number = MAX_JSON_BYTES,
): boolean {
  const raw = request.headers.get("Content-Length");
  if (raw === null) {
    return false;
  }
  const length = Number(raw);
  return Number.isFinite(length) && length > maxBytes;
}

export function isPublicEdgePath(method: string, pathname: string): boolean {
  if (method !== "GET" && method !== "HEAD") {
    return false;
  }
  return (
    pathname === "/" || pathname === "/health" || pathname === "/ready"
  );
}

export function rateLimitKey(
  request: Request,
  actor: ActorClass,
  route: RouteClass,
): string {
  return `${actor}:${request.method}:${route}`;
}

export async function enforceRateLimit(
  limiter: RateLimit,
  key: string,
): Promise<boolean> {
  const outcome = await limiter.limit({ key });
  return outcome.success;
}
