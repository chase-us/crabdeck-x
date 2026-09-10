export const HTTP_ORIGIN_TIMEOUT_MS = 10_000;

const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
]);

function joinOrigin(originBase: string, pathname: string, search: string): URL {
  const origin = new URL(originBase);
  const normalizedPath = pathname.startsWith("/") ? pathname : `/${pathname}`;
  const prefix =
    origin.pathname === "/" || origin.pathname === ""
      ? ""
      : origin.pathname.replace(/\/$/, "");
  return new URL(`${prefix}${normalizedPath}${search}`, origin.origin);
}

function copyHeaders(source: Headers, inboundWebSocket: boolean): Headers {
  const headers = new Headers();
  for (const [name, value] of source.entries()) {
    if (!inboundWebSocket && HOP_BY_HOP.has(name.toLowerCase())) {
      continue;
    }
    headers.append(name, value);
  }
  return headers;
}

/**
 * Stream the inbound request to an origin. Do not buffer `request.text()` /
 * `arrayBuffer()` — Workers have a 128 MB memory limit.
 *
 * WebSocket upgrades are passed through `fetch(request)` so the runtime holds
 * the tunnel. The Node gateway remains the agent bus; a Durable Object would
 * duplicate that mesh. See Workers best practices: DO hibernation is for
 * sockets the Worker itself accepts with `ctx.acceptWebSocket()`.
 */
export async function proxyToOrigin(
  request: Request,
  originBase: string,
  pathname: string,
  requestId: string,
): Promise<Response> {
  const inbound = new URL(request.url);
  const target = joinOrigin(originBase, pathname, inbound.search);
  const inboundWebSocket =
    request.headers.get("Upgrade")?.toLowerCase() === "websocket";
  const headers = copyHeaders(request.headers, inboundWebSocket);
  headers.set("X-Request-Id", requestId);

  let response: Response;
  if (inboundWebSocket) {
    response = await fetch(
      new Request(target, {
        method: request.method,
        headers,
        body: request.body,
        redirect: "manual",
      }),
    );
  } else {
    response = await fetch(target, {
      method: request.method,
      headers,
      body: request.body,
      redirect: "manual",
      signal: AbortSignal.timeout(HTTP_ORIGIN_TIMEOUT_MS),
    });
  }
  const outHeaders = copyHeaders(response.headers, inboundWebSocket);
  outHeaders.set("X-Request-Id", requestId);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: outHeaders,
  });
}

export async function probeOriginHealth(
  originBase: string,
): Promise<{ ok: boolean; error?: string }> {
  const target = joinOrigin(originBase, "/health", "");
  try {
    const response = await fetch(target, {
      method: "GET",
      signal: AbortSignal.timeout(1500),
    });
    // Bounded health JSON from our own origin — safe to inspect status only.
    return { ok: response.ok };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { ok: false, error: message };
  }
}
