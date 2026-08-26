import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Frame Intelligence",
  description: "Videolarınızdan seçilmiş kareler üretin.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="tr">
      <body>
        <header className="site-header">
          <Link className="brand" href="/" aria-label="Frame Intelligence ana sayfa">
            <span aria-hidden="true" className="brand-mark">FI</span>
            <span>Frame Intelligence</span>
          </Link>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
