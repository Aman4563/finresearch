import type { Metadata } from "next";

import { Lesson } from "../_lib/lesson";
import { M1 } from "../_lib/m1-basics";

export const metadata: Metadata = { title: `Learn F&O · ${M1.title}` };

export default function Page() {
  return <Lesson m={M1} />;
}
