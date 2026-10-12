"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

type AuthMode = "login" | "register";

type ErrorResponse = {
  detail?: string;
};

export function AuthClient() {
  const router = useRouter();
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [checkingSession, setCheckingSession] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    async function checkSession() {
      try {
        const response = await fetch("/api/v1/auth/me", {
          credentials: "same-origin",
          cache: "no-store",
          signal: controller.signal,
        });

        if (response.ok) {
          router.replace("/brands");
          return;
        }
      } catch {
        // Giriş ekranı ağ hatasında da kullanılabilir durumda kalır.
      } finally {
        if (!controller.signal.aborted) {
          setCheckingSession(false);
        }
      }
    }

    void checkSession();

    return () => controller.abort();
  }, [router]);

  function changeMode(nextMode: AuthMode) {
    setMode(nextMode);
    setError(null);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);

    try {
      const response = await fetch(`/api/v1/auth/${mode}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "content-type": "application/json",
        },
        body: JSON.stringify({
          email,
          password,
        }),
      });

      if (response.ok) {
        router.push("/brands");
        return;
      }

      let body: ErrorResponse = {};
      try {
        body = (await response.json()) as ErrorResponse;
      } catch {
        // JSON olmayan hata yanıtlarında aşağıdaki genel mesaj kullanılır.
      }

      if (mode === "login" && response.status === 401) {
        setError("E-posta adresi veya şifre hatalı.");
      } else if (mode === "register" && response.status === 409) {
        setError("Bu e-posta adresiyle daha önce bir hesap oluşturulmuş.");
      } else if (response.status === 422) {
        setError(
          mode === "register"
            ? "Geçerli bir e-posta adresi ve en az 8 karakterlik bir şifre girin."
            : "E-posta adresinizi ve şifrenizi kontrol edin.",
        );
      } else {
        setError(body.detail ?? "İşlem tamamlanamadı. Lütfen tekrar deneyin.");
      }
    } catch {
      setError("Sunucuya ulaşılamadı. Lütfen tekrar deneyin.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="auth-shell">
      <section className="auth-intro" aria-labelledby="auth-title">
        <p className="eyebrow">Frame Intelligence</p>
        <h1 id="auth-title">
          Görsel verilerinizi tek bir çalışma alanında yönetin.
        </h1>
        <p className="hero-copy">
          Veri setlerinizi, etiketleme çalışmalarınızı ve model süreçlerinizi
          hesabınıza bağlı güvenli bir çalışma alanında takip edin.
        </p>
      </section>

      <section className="auth-card" aria-label="Hesap işlemleri">
        <div className="auth-tabs" role="tablist" aria-label="Hesap işlemi seçimi">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "login"}
            onClick={() => changeMode("login")}
            disabled={pending}
          >
            Giriş Yap
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "register"}
            onClick={() => changeMode("register")}
            disabled={pending}
          >
            Hesap Oluştur
          </button>
        </div>

        <div className="auth-heading">
          <p className="eyebrow">
            {mode === "login" ? "Tekrar hoş geldiniz" : "Yeni hesap"}
          </p>
          <h2>
            {mode === "login" ? "Hesabınıza giriş yapın" : "Hesabınızı oluşturun"}
          </h2>
          <p>
            {mode === "login"
              ? "Çalışma alanınıza devam etmek için bilgilerinizi girin."
              : "Frame Intelligence çalışma alanınızı oluşturmak için bilgilerinizi girin."}
          </p>
        </div>

        {checkingSession ? (
          <div className="auth-session-check" role="status">
            <span className="spinner" aria-hidden="true" />
            Oturum kontrol ediliyor…
          </div>
        ) : (
          <form onSubmit={submit}>
            {error ? (
              <div className="alert error" role="alert">
                {error}
              </div>
            ) : null}

            <div className="field">
              <label htmlFor="auth-email">E-posta adresi</label>
              <input
                id="auth-email"
                name="email"
                type="email"
                autoComplete="email"
                inputMode="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="ornek@email.com"
                required
                disabled={pending}
                maxLength={320}
              />
            </div>

            <div className="field">
              <label htmlFor="auth-password">Şifre</label>
              <input
                id="auth-password"
                name="password"
                type="password"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                disabled={pending}
                minLength={mode === "register" ? 8 : 1}
                maxLength={128}
              />
              {mode === "register" ? (
                <span className="hint">En az 8 karakter kullanın.</span>
              ) : null}
            </div>

            <button className="button primary auth-submit" type="submit" disabled={pending}>
              {pending
                ? "İşleniyor…"
                : mode === "login"
                  ? "Giriş Yap"
                  : "Hesap Oluştur"}
            </button>
          </form>
        )}

        <p className="auth-security-note">
          Oturum bilgisi tarayıcıda JavaScript tarafından okunamayan güvenli bir
          HttpOnly cookie ile saklanır.
        </p>
      </section>
    </div>
  );
}