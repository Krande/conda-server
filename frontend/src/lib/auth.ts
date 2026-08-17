// Auth is modeled as a React Query for /api/auth/me. No localStorage,
// no custom events — the query cache IS the source of truth, and
// window-focus refetches keep it fresh.

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ApiError, api } from "./http";
import type { User } from "./types";

const ME_KEY = ["auth", "me"] as const;

export function useCurrentUser() {
  const query = useQuery<User | null>({
    queryKey: ME_KEY,
    queryFn: async () => {
      try {
        return await api<User>("/auth/me");
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null;
        throw err;
      }
    },
    staleTime: 60_000,
  });

  return {
    user: query.data ?? null,
    isLoggedIn: !!query.data,
    isAdmin: query.data?.role === "admin",
    isLoading: query.isLoading,
    error: query.error,
  };
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<void>("/auth/logout", { method: "POST" }),
    onSuccess: () => {
      qc.setQueryData(ME_KEY, null);
      qc.invalidateQueries();
    },
  });
}

export function loginRedirectUrl(returnTo?: string): string {
  const qs = returnTo ? `?redirect=${encodeURIComponent(returnTo)}` : "";
  return `/api/auth/login${qs}`;
}
