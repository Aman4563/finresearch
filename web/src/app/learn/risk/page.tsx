import type { Metadata } from "next";

import { Lesson } from "../_lib/lesson";
import { M4 } from "../_lib/m4-risk";

export const metadata: Metadata = { title: `Learn F&O · ${M4.title}` };

export default function Page() {
  return <Lesson m={M4} />;
}
