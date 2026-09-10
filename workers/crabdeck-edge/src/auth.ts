const encoder = new TextEncoder();

/**
 * Constant-time comparison for secrets. Hash to a fixed size first so a
 * length mismatch cannot leak the expected token via timing.
 * https://developers.cloudflare.com/workers/runtime-apis/web-crypto/
 */
export async function tokensMatch(
  provided: string,
  expected: string,
): Promise<boolean> {
  const [providedHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(provided)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(providedHash, expectedHash);
}

export function extractPresentedToken(request: Request): string | null {
  const authorization = request.headers.get("Authorization");
  if (authorization?.startsWith("Bearer ")) {
    const token = authorization.slice("Bearer ".length).trim();
    return token.length > 0 ? token : null;
  }
  const header = request.headers.get("X-Edge-Token");
  if (header === null) {
    return null;
  }
  const trimmed = header.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export function parseAllowedOrigins(raw: string): ReadonlySet<string> {
  return new Set(
    raw
      .split(",")
      .map((part) => part.trim())
      .filter((part) => part.length > 0),
  );
}

export function originAllowed(
  request: Request,
  allowed: ReadonlySet<string>,
): boolean {
  const origin = request.headers.get("Origin");
  if (origin === null || origin.length === 0) {
    return true;
  }
  return allowed.has(origin);
}
