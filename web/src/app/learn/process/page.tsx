import type { Metadata } from "next";

import { Lesson } from "../_lib/lesson";
import { M5 } from "../_lib/m5-process";

export const metadata: Metadata = { title: `Learn F&O · ${M5.title}` };

export default function Page() {
  return <Lesson m={M5} />;
}
