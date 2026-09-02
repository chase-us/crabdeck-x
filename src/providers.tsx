import { AuthKitProvider, useAuth } from "@workos-inc/authkit-react";
import { ConvexProvider, ConvexProviderWithAuth, ConvexReactClient } from "convex/react";
import { useCallback, useMemo, type ReactNode } from "react";

const convexUrl = import.meta.env.VITE_CONVEX_URL;
const workosClientId = import.meta.env.VITE_WORKOS_CLIENT_ID;

const convex = convexUrl ? new ConvexReactClient(convexUrl) : null;

function useAuthFromWorkOS() {
  const { isLoading, user, getAccessToken } = useAuth();

  const fetchAccessToken = useCallback(
    async ({ forceRefreshToken }: { forceRefreshToken: boolean }) => {
      try {
        const token = await getAccessToken({ forceRefresh: forceRefreshToken });
        return token ?? null;
      } catch {
        return null;
      }
    },
    [getAccessToken],
  );

  return useMemo(
    () => ({
      isLoading,
      isAuthenticated: Boolean(user),
      fetchAccessToken,
    }),
    [fetchAccessToken, isLoading, user],
  );
}

function AuthenticatedConvex({ children }: { children: ReactNode }) {
  if (!convex) {
    return <>{children}</>;
  }

  return (
    <ConvexProviderWithAuth client={convex} useAuth={useAuthFromWorkOS}>
      {children}
    </ConvexProviderWithAuth>
  );
}

export function AppProviders({ children }: { children: ReactNode }) {
  if (!convex) {
    return <>{children}</>;
  }

  if (!workosClientId) {
    return <ConvexProvider client={convex}>{children}</ConvexProvider>;
  }

  return (
    <AuthKitProvider
      clientId={workosClientId}
      redirectUri={window.location.origin}
    >
      <AuthenticatedConvex>{children}</AuthenticatedConvex>
    </AuthKitProvider>
  );
}
