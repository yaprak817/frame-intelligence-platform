import type { Metadata } from "next";

import { AuthClient } from "./auth-client";

export const metadata: Metadata = {
  title: "Giriş Yap | Frame Intelligence",
  description: "Frame Intelligence hesabınıza giriş yapın veya yeni hesap oluşturun.",
};

export default function LoginPage() {
  return <AuthClient />;
}