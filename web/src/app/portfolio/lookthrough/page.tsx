import type { Metadata } from "next";

import { LookthroughPanel } from "@/components/portfolio/lookthrough-panel";

export const metadata: Metadata = { title: "Look-through" };

export default function Page() {
  return <LookthroughPanel />;
}
