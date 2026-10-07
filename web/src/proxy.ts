// Hands the browser the local API token (issue #246) as an httpOnly, SameSite=Strict cookie for 127.0.0.1.
//
// `finresearch serve` writes the token to data/state/api_token (mode 0600). This proxy runs in the Next.js server
// (Node runtime), reads that file and sets `finresearch_token_<api port>` on page responses. Cookies are scoped to the
// host, not the port, so the browser then sends the cookie with its calls to the API on 127.0.0.1:8710 (api.ts uses
// credentials: "include"; the API's CORS allows credentials for the dashboard origins only). The token never reaches
// page JavaScript.
//
// A cookie set for "localhost" would not be sent to 127.0.0.1, so a visit to localhost is redirected to 127.0.0.1.
import { readFileSync, statSync } from "node:fs";
import path from "node:path";
import { NextResponse, type NextRequest } from "next/server";

const API_URL = new URL(process.env.NEXT_PUBLIC_FINRESEARCH_API ?? "http://127.0.0.1:8710");
const COOKIE = `finresearch_token_${API_URL.port || (API_URL.protocol === "https:" ? "443" : "80")}`;
const TOKEN_FILE =
  process.env.FINRESEARCH_API_TOKEN_FILE ??
  path.join(process.env.FINRESEARCH_STATE_DIR ?? path.join(process.cwd(), "..", "data", "state"), "api_token");

let cached: { mtime: number; token: string | null } | null = null;

/** The token, re-read when the file changes; null while the API has never started (the API then answers 401). */
function readToken(): string | null {
  try {
    const mtime = statSync(/* turbopackIgnore: true */ TOKEN_FILE).mtimeMs;
    if (cached?.mtime !== mtime) cached = { mtime, token: readFileSync(/* turbopackIgnore: true */ TOKEN_FILE, "utf8").trim() || null };
    return cached.token;
  } catch {
    cached = null;
    return null;
  }
}

export function proxy(request: NextRequest) {
  // the Host header, not nextUrl: Next normalises nextUrl's host to "localhost" in development
  const [host, port] = (request.headers.get("host") ?? "").split(":");
  if (host === "localhost" && API_URL.hostname === "127.0.0.1") {
    const { pathname, search } = request.nextUrl;
    // not a 3xx: Next treats localhost and 127.0.0.1 as one origin and rewrites such a Location to a relative URL
    // (NextURL normalises loopback hosts), which would loop; a refresh page moves the browser instead
    const safePort = /^\d{1,5}$/.test(port ?? "") ? `:${port}` : ""; // never anything but a port after the host
    const to = `http://127.0.0.1${safePort}${pathname}${search}`.replace(/[<>"&]/g, "");
    const html = `<!doctype html><meta http-equiv="refresh" content="0;url=${to}"><a href="${to}">Continue to ${to}</a>`;
    return new NextResponse(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
  }
  const response = NextResponse.next();
  const token = readToken();
  if (token && request.cookies.get(COOKIE)?.value !== token) {
    response.cookies.set({ name: COOKIE, value: token, httpOnly: true, sameSite: "strict", path: "/", secure: false });
  }
  return response;
}

export const config = {
  // pages and their data requests; not the static bundles, images or the pdf.js assets
  matcher: ["/((?!_next/static|_next/image|favicon.ico|pdfjs/).*)"],
};
