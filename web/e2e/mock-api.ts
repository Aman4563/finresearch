// The mocked API layer for the smoke tests: every request to any host's /api/ path is answered here, so no real
// FinResearch API (and no personal data) is ever reached. A route is "METHOD /path" (query string ignored) mapped to
// a JSON body, or to [status, body]. A request with no route gets a 404 and is recorded: tests call
// `expectAllRouted()` with the paths a page may leave unanswered (its optional cards then show their error state).
import { type Page, expect } from "@playwright/test";

export type Routes = Record<string, unknown>;

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, HEAD, POST, PUT, PATCH, DELETE",
  "access-control-allow-headers": "*",
};

/** Shell calls every page makes (health dot, alert badge, profile name). */
export const SHELL: Routes = {
  "GET /api/health": { ok: true, version: "0.0.0-e2e" },
  "GET /api/alerts": [],
  "GET /api/profile": [404, { detail: "no profile in the e2e fixtures" }],
};

export async function mockApi(page: Page, routes: Routes) {
  const all: Routes = { ...SHELL, ...routes };
  const unrouted: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // the first-run welcome tour would cover the page: mark it seen, as after a first visit (components/help/welcome.tsx)
  await page.addInitScript((key) => localStorage.setItem(key, "done"), "finresearch.welcome");
  await page.route(/\/api\//, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const key = `${req.method()} ${url.pathname}`;
    if (!(key in all)) {
      unrouted.push(key);
      return route.fulfill({ status: 404, headers: CORS, contentType: "application/json", body: JSON.stringify({ detail: "not in the e2e fixtures" }) });
    }
    const hit = all[key];
    const [status, body] = Array.isArray(hit) && hit.length === 2 && typeof hit[0] === "number" ? hit : [200, hit];
    return route.fulfill({ status, headers: CORS, contentType: "application/json", body: JSON.stringify(body) });
  });
  return {
    /** No uncaught page error, and every unanswered API call is one this page may leave unanswered. */
    expectClean(optional: string[] = []) {
      expect(errors, "uncaught errors in the page").toEqual([]);
      expect([...new Set(unrouted)].filter((k) => !optional.some((o) => k.startsWith(o))), "API calls with no fixture").toEqual([]);
    },
  };
}

/** No horizontal page scroll: the document is no wider than the viewport. */
export async function expectNoHorizontalOverflow(page: Page) {
  const { scroll, client } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(scroll, `page is ${scroll - client}px wider than the ${client}px viewport`).toBeLessThanOrEqual(client);
}
