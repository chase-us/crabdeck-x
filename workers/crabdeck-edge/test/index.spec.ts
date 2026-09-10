import {
  createExecutionContext,
  waitOnExecutionContext,
} from "cloudflare:test";
import { env } from "cloudflare:workers";
import { describe, expect, it } from "vitest";
import { extractPresentedToken, tokensMatch } from "../src/auth";
import worker from "../src/index";
import {
  classifyRoute,
  contentLengthExceeds,
  rateLimitKey,
  stripPrefix,
} from "../src/limits";

const IncomingRequest = Request<unknown, IncomingRequestCfProperties>;

async function handle(request: Request): Promise<Response> {
  const ctx = createExecutionContext();
  const response = await worker.fetch(request, env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

describe("crabdeck-edge", () => {
  it("answers GET /health without contacting origin", async () => {
    const response = await handle(
      new IncomingRequest("https://hermesclaw.ai/health"),
    );
    expect(response.status).toBe(200);
    const body: unknown = await response.json();
    expect(body).toMatchObject({ status: "ok", edge: true, worker: "crabdeck-edge" });
    expect(response.headers.get("X-Request-Id")).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
  });

  it("answers GET / so Cloudflare probes are not 521", async () => {
    const response = await handle(new IncomingRequest("https://hermesclaw.ai/"));
    expect(response.status).toBe(200);
    const body: unknown = await response.json();
    expect(body).toMatchObject({ status: "ok", edge: true });
  });

  it("returns 503 on /ready when origin is down", async () => {
    const response = await handle(
      new IncomingRequest("https://hermesclaw.ai/ready"),
    );
    expect(response.status).toBe(503);
    const body: unknown = await response.json();
    expect(body).toMatchObject({ status: "origin_unreachable", edge: true });
  });

  it("rejects disallowed browser origins", async () => {
    const response = await handle(
      new IncomingRequest("https://hermesclaw.ai/health", {
        headers: { Origin: "https://evil.example" },
      }),
    );
    expect(response.status).toBe(403);
  });

  it("rejects oversized JSON before buffering", async () => {
    const response = await handle(
      new IncomingRequest("https://hermesclaw.ai/gw/metrics", {
        method: "POST",
        headers: {
          "Content-Length": String(64 * 1024),
          Authorization: "Bearer test-edge-token",
        },
      }),
    );
    expect(response.status).toBe(413);
  });

  it("requires EDGE_TOKEN on proxied HTTP routes", async () => {
    const missing = await handle(
      new IncomingRequest("https://hermesclaw.ai/gw/health"),
    );
    expect(missing.status).toBe(401);

    const bad = await handle(
      new IncomingRequest("https://hermesclaw.ai/gw/health", {
        headers: { Authorization: "Bearer wrong-token" },
      }),
    );
    expect(bad.status).toBe(401);
  });

  it("returns 502 rather than throwing when origin fetch fails", async () => {
    const response = await handle(
      new IncomingRequest("https://hermesclaw.ai/gw/health", {
        headers: { Authorization: "Bearer test-edge-token" },
      }),
    );
    expect(response.status).toBe(502);
    const body: unknown = await response.json();
    expect(body).toMatchObject({ error: "Origin unreachable", edge: true });
  });

  it("returns 404 for unknown paths", async () => {
    const response = await handle(
      new IncomingRequest("https://hermesclaw.ai/nope", {
        headers: { Authorization: "Bearer test-edge-token" },
      }),
    );
    expect(response.status).toBe(404);
  });
});

describe("auth helpers", () => {
  it("compares tokens in constant time", async () => {
    expect(await tokensMatch("abc", "abc")).toBe(true);
    expect(await tokensMatch("abc", "xyz")).toBe(false);
  });

  it("reads Bearer and X-Edge-Token", () => {
    expect(
      extractPresentedToken(
        new Request("https://example.com", {
          headers: { Authorization: "Bearer secret" },
        }),
      ),
    ).toBe("secret");
    expect(
      extractPresentedToken(
        new Request("https://example.com", {
          headers: { "X-Edge-Token": "header-secret" },
        }),
      ),
    ).toBe("header-secret");
  });
});

describe("route helpers", () => {
  it("classifies and strips vite-style prefixes", () => {
    expect(classifyRoute("/health")).toBe("edge");
    expect(classifyRoute("/gw/metrics")).toBe("gw");
    expect(stripPrefix("/gw/metrics", "/gw")).toBe("/metrics");
    expect(stripPrefix("/ws", "/ws")).toBe("/");
    expect(rateLimitKey(new Request("https://x/gw"), "anon", "gw")).toBe(
      "anon:GET:gw",
    );
    expect(
      contentLengthExceeds(
        new Request("https://x", { headers: { "Content-Length": "10" } }),
        32,
      ),
    ).toBe(false);
  });
});
