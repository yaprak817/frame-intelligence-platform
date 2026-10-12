"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

type CurrentUser = {
  id: string;
  email: string;
  created_at: string;
};

export function SessionControls() {
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    fetch("/api/v1/auth/me", {
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          setUser(null);
          return;
        }

        const data = (await response.json()) as CurrentUser;
        setUser(data);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setUser(null);
        }
      });

    return () => controller.abort();
  }, []);

  async function logout() {
    if (busy) return;

    setBusy(true);
    setError(null);

    try {
      const response = await fetch("/api/v1/auth/logout", {
        method: "POST",
        credentials: "same-origin",
      });

      if (!response.ok) {
        throw new Error("Logout failed");
      }

      setUser(null);
      router.replace("/login");
      router.refresh();
    } catch {
      setError("??k?? yap?lamad?. L?tfen tekrar deneyin.");
      setBusy(false);
    }
  }

  if (!user) {
    return null;
  }

  return (
    <div className="session-controls">
      <span className="session-email" title={user.email}>
        {user.email}
      </span>
      <button
        className="session-logout"
        type="button"
        disabled={busy}
        onClick={logout}
      >
        {busy ? "??k?? yap?l?yor?" : "??k?? yap"}
      </button>
      {error ? (
        <span className="session-error" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
