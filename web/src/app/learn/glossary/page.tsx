import type { Metadata } from "next";

import { GlossaryView } from "../_lib/glossary-view";

export const metadata: Metadata = { title: "Learn F&O · Glossary" };

export default function Page() {
  return <GlossaryView />;
}
