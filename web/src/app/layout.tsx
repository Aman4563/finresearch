import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import { ViewTransition } from "react";

import { AppShell, THEME_SCRIPT } from "@/components/shell";
import { PRIVACY_SCRIPT } from "@/lib/privacy-script";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jetbrains", display: "swap" });

export const metadata: Metadata = {
  title: { default: "FinResearch", template: "%s · FinResearch" },
  description: "Personal, fact-checked research for IPOs, stocks, funds, bonds and F&O",
  applicationName: "FinResearch",
  // installable as an app (PWA) from the browser on this Mac: manifest + icons in web/public
  manifest: "/manifest.webmanifest",
  icons: { icon: [{ url: "/icon-192.png", sizes: "192x192", type: "image/png" }], apple: "/apple-touch-icon.png" },
  appleWebApp: { capable: true, title: "FinResearch", statusBarStyle: "black-translucent" },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f5f7fb" },
    { media: "(prefers-color-scheme: dark)", color: "#070b14" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable} h-full antialiased`} suppressHydrationWarning>
      <head>
        {/* set the theme before first paint, so a dark-mode viewer never sees a white flash */}
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
        {/* and blur amounts before first paint when the privacy toggle is on */}
        <script dangerouslySetInnerHTML={{ __html: PRIVACY_SCRIPT }} />
      </head>
      <body className="min-h-full">
        <AppShell>
          <ViewTransition name="page">{children}</ViewTransition>
        </AppShell>
      </body>
    </html>
  );
}
