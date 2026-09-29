import { API_URL } from "@/lib/api";

/** In-app viewer link: a file of a run's pack (`run` + pack-relative `path`) or a source document at a page. */
export function viewerHref(t: { run: number; path: string } | { doc: number; page?: number | null }): string {
  if ("run" in t) return `/viewer?run=${t.run}&path=${encodeURIComponent(t.path)}`;
  return t.page ? `/viewer?doc=${t.doc}&page=${t.page}` : `/viewer?doc=${t.doc}`;
}

/** The API URL of the same file (inline; `download` makes the browser save it). Pack path segments are encoded. */
export function fileUrl(t: { run: number; path: string } | { doc: number }, download = false): string {
  const base =
    "run" in t
      ? `${API_URL}/api/runs/${t.run}/pack/${t.path.split("/").map(encodeURIComponent).join("/")}`
      : `${API_URL}/api/documents/${t.doc}/file`;
  return download ? `${base}?download=1` : base;
}
