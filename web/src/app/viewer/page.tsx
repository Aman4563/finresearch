import type { Metadata } from "next";

import { Viewer, type ViewerTarget } from "@/components/viewer/viewer";

type Search = Record<string, string | string[] | undefined>;

const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
const posInt = (v: string | string[] | undefined) => {
  const n = Number(one(v));
  return Number.isInteger(n) && n > 0 ? n : null;
};

/** `?run=<id>&path=<pack-relative path>` or `?doc=<document id>&page=<n>` (see lib/viewer.ts). */
function target(sp: Search): ViewerTarget {
  const run = posInt(sp.run);
  const path = one(sp.path);
  if (run && path) return { run, path: path.replace(/^\/+/, "") };
  const doc = posInt(sp.doc);
  if (doc) return { doc, page: posInt(sp.page) };
  return null;
}

export async function generateMetadata({ searchParams }: PageProps<"/viewer">): Promise<Metadata> {
  const t = target(await searchParams);
  if (!t) return { title: "Viewer" };
  return { title: "run" in t ? (t.path.split("/").pop() ?? t.path) : `Document ${t.doc}` };
}

export default async function ViewerPage({ searchParams }: PageProps<"/viewer">) {
  const t = target(await searchParams);
  // a new file remounts the viewer; a new page of the same document too (it opens there)
  const key = !t ? "none" : "run" in t ? `run-${t.run}-${t.path}` : `doc-${t.doc}-${t.page ?? 1}`;
  return <Viewer key={key} target={t} />;
}
