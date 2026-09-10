import {
  extractPresentedToken,
  originAllowed,
  parseAllowedOrigins,
  tokensMatch,
} from "./auth";
import {
  classifyRoute,
  contentLengthExceeds,
  enforceRateLimit,
  isPublicEdgePath,
  rateLimitKey,
  stripPrefix,
} from "./limits";
import { logJson, type LogFields } from "./log";
import { probeOriginHealth, proxyToOrigin } from "./proxy";

async function recordAccess(
  level: "log" | "warn" | "error",
  fields: LogFields,
): Promise<void> {
  logJson(level, fields);
}

function jsonError(
  status: number,
  error: string,
  requestId: string,
): Response {
  return Response.json(
    { error, requestId, edge: true },
    {
      status,
      headers: { "X-Request-Id": requestId },
    },
  );
}

function edgePayload(
  requestId: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    status: "ok",
    edge: true,
    worker: "crabdeck-edge",
    requestId,
    ...extra,
  };
}

async function requireEdgeToken(
  request: Request,
  env: Env,
  requestId: string,
): Promise<Response | null> {
  const expected = env.EDGE_TOKEN;
  if (expected === undefined || expected.length === 0) {
    return null;
  }
  const presented = extractPresentedToken(request);
  if (presented === null) {
    return jsonError(401, "Missing edge token", requestId);
  }
  const ok = await tokensMatch(presented, expected);
  if (!ok) {
    logJson("warn", {
      message: "edge token rejected",
      requestId,
      path: new URL(request.url).pathname,
    });
    return jsonError(401, "Invalid edge token", requestId);
  }
  return null;
}

export default {
  async fetch(
    request: Request,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<Response> {
    const requestId = crypto.randomUUID();
    const url = new URL(request.url);

    try {
      logJson("log", {
        message: "incoming request",
        requestId,
        method: request.method,
        path: url.pathname,
      });

      const allowed = parseAllowedOrigins(env.ALLOWED_ORIGINS);
      if (!originAllowed(request, allowed)) {
        logJson("warn", {
          message: "origin rejected",
          requestId,
          path: url.pathname,
        });
        return jsonError(403, "Origin not allowed", requestId);
      }

      if (contentLengthExceeds(request)) {
        return jsonError(413, "Payload too large", requestId);
      }

      const isWebSocket =
        request.headers.get("Upgrade")?.toLowerCase() === "websocket";
      const route = isWebSocket ? "ws" : classifyRoute(url.pathname);
      const presented = extractPresentedToken(request);
      const actor =
        presented !== null && env.EDGE_TOKEN !== undefined && env.EDGE_TOKEN.length > 0
          ? "authed"
          : "anon";

      const allowedByLimit = await enforceRateLimit(
        env.EDGE_RATE_LIMITER,
        rateLimitKey(request, actor, route),
      );
      if (!allowedByLimit) {
        logJson("warn", {
          message: "rate limited",
          requestId,
          path: url.pathname,
          status: 429,
        });
        return jsonError(429, "Rate limit exceeded", requestId);
      }

      if (route === "edge") {
        if (url.pathname === "/ready") {
          const probe = await probeOriginHealth(env.ORIGIN_URL);
          const status = probe.ok ? 200 : 503;
          const body = edgePayload(requestId, {
            status: probe.ok ? "ok" : "origin_unreachable",
            origin: env.ORIGIN_URL,
          });
          if (!probe.ok) {
            ctx.waitUntil(
              recordAccess("error", {
                message: "origin probe failed",
                requestId,
                path: "/ready",
                error: probe.error ?? "unhealthy",
              }),
            );
          }
          return Response.json(body, {
            status,
            headers: { "X-Request-Id": requestId },
          });
        }
        return Response.json(edgePayload(requestId, { path: url.pathname }), {
          status: 200,
          headers: { "X-Request-Id": requestId },
        });
      }

      if (!isPublicEdgePath(request.method, url.pathname) && !isWebSocket) {
        const denied = await requireEdgeToken(request, env, requestId);
        if (denied !== null) {
          return denied;
        }
      }

      let originBase: string;
      let originPath: string;
      switch (route) {
        case "ws":
        case "gw":
          originBase = env.ORIGIN_URL;
          originPath =
            route === "ws"
              ? stripPrefix(url.pathname, "/ws")
              : stripPrefix(url.pathname, "/gw");
          break;
        case "vault":
          originBase = env.VAULT_ORIGIN_URL;
          originPath = stripPrefix(url.pathname, "/vault");
          break;
        case "api":
          originBase = env.ORCHESTRATOR_ORIGIN_URL;
          originPath = stripPrefix(url.pathname, "/api");
          break;
        case "other":
          return jsonError(404, "Not found", requestId);
        default: {
          const exhaustive: never = route;
          return jsonError(
            500,
            `Unhandled route: ${String(exhaustive)}`,
            requestId,
          );
        }
      }

      try {
        const proxied = await proxyToOrigin(
          request,
          originBase,
          originPath,
          requestId,
        );
        ctx.waitUntil(
          recordAccess("log", {
            message: "origin proxied",
            requestId,
            path: url.pathname,
            status: proxied.status,
          }),
        );
        return proxied;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        logJson("error", {
          message: "origin fetch failed",
          requestId,
          path: url.pathname,
          error: message,
        });
        return jsonError(502, "Origin unreachable", requestId);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      logJson("error", {
        message: "unhandled error",
        requestId,
        path: url.pathname,
        error: message,
      });
      return jsonError(500, "Internal server error", requestId);
    }
  },
} satisfies ExportedHandler<Env>;
