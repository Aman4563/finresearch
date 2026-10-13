import type { Metadata } from "next";

import { Lesson } from "../_lib/lesson";
import { M3 } from "../_lib/m3-strategies";

export const metadata: Metadata = { title: `Learn F&O · ${M3.title}` };

export default function Page() {
  return <Lesson m={M3} />;
}
