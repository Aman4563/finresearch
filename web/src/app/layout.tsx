import type { Metadata } from "next";
import Link from "next/link";

import { AlertBadge } from "@/components/alerts";
import "./globals.css";

export const metadata: Metadata = {
  title: "FinResearch",
  description: "Personal, fact-checked IPO research",
};

const nav = [
  { href: "/", label: "IPO radar" },
  { href: "/stocks", label: "Stocks" },
  { href: "/funds", label: "Funds" },
  { href: "/runs", label: "Runs" },
  { href: "/monitor", label: "Monitor" },
  { href: "/journal", label: "Journal" },
  { href: "/profile", label: "Profile & rules" },
  { href: "/usage", label: "Usage" },
];

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        <header className="border-b border-border bg-card">
          <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
            <Link href="/" className="font-semibold tracking-tight">
              FinResearch
            </Link>
            <nav className="flex gap-4 text-sm text-muted">
              {nav.map((n) => (
                <Link key={n.href} href={n.href} className="hover:text-foreground">
                  {n.label}
                </Link>
              ))}
            </nav>
            <div className="ml-auto">
              <AlertBadge />
            </div>
          </div>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
