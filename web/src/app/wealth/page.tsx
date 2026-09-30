import type { Metadata } from "next";

import { WealthPage } from "@/components/wealth/wealth-page";

export const metadata: Metadata = { title: "Wealth" };

export default function Page() {
  return <WealthPage />;
}
