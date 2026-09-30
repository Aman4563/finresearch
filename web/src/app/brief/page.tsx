import type { Metadata } from "next";

import { BriefPage } from "@/components/brief/brief-page";

export const metadata: Metadata = { title: "Morning brief" };

export default function Page() {
  return <BriefPage />;
}
