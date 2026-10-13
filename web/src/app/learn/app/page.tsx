import type { Metadata } from "next";

import { Lesson } from "../_lib/lesson";
import { M6 } from "../_lib/m6-app";

export const metadata: Metadata = { title: `Learn F&O · ${M6.title}` };

export default function Page() {
  return <Lesson m={M6} />;
}
