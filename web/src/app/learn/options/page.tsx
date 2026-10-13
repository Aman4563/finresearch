import type { Metadata } from "next";

import { Lesson } from "../_lib/lesson";
import { M2 } from "../_lib/m2-options";

export const metadata: Metadata = { title: `Learn F&O · ${M2.title}` };

export default function Page() {
  return <Lesson m={M2} />;
}
