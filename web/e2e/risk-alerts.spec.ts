// #398: a risk alert in the feed names the series it read and takes one tap, "acted" or "ignored" (a POST); a
// "can't check" risk alert is not scored, and an answered alert shows the answer instead of the buttons. Synthetic
// alerts only.
import { expect, test } from "@playwright/test";

import { mockApi } from "./mock-api";

const at = new Date(Date.now() - 3600_000).toISOString();
const FIRED = {
  id: 501, watch_id: null, kind: "risk_alert", level: "warn", created_at: at, read_at: null,
  message: "IV30 shock (one session) (NIFTY): 3.40 >= 3.00 (registered level); synthetic. An alert only: nothing was traded.",
  data: { rule: "iv_shock", status: "fired", tier: "warn", series: "fno_iv_eod.iv_30d NIFTY (synthetic)", path: "/fno" },
};
const UNKNOWN = {
  id: 502, watch_id: null, kind: "risk_alert", level: "warn", created_at: at, read_at: null,
  message: "Book expected shortfall vs its limit (the F&O book): can't check, the input is unknown: not computed yet (P3.4).",
  data: { rule: "es", status: "unknown", tier: "urgent", series: "no expected-shortfall series", path: "/fno" },
};
const ANSWERED = { ...FIRED, id: 503, message: "Overnight gap (NIFTY): 3.10 >= 3.00", data: { ...FIRED.data, rule: "gap", feedback: "ignored" } };

test("risk alerts: one tap records acted, can't-check alerts are not scored", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/alerts": [FIRED, UNKNOWN, ANSWERED],
    "GET /api/watches": [],
    "GET /api/companies": [],
    "POST /api/alerts/501/feedback": { ...FIRED, read_at: at, data: { ...FIRED.data, feedback: "acted" } },
  });
  await page.goto("/monitor");
  const fired = page.getByRole("listitem").filter({ hasText: "IV30 shock" });
  await expect(fired.getByText("series: fno_iv_eod.iv_30d NIFTY (synthetic)")).toBeVisible();
  const unknown = page.getByRole("listitem").filter({ hasText: "can't check" });
  await expect(unknown.getByRole("button", { name: "Acted" })).toHaveCount(0);
  await expect(page.getByRole("listitem").filter({ hasText: "Overnight gap" }).getByText("You marked this alert: ignored.")).toBeVisible();
  await fired.getByRole("button", { name: "Acted" }).click();
  await expect.poll(() => api.count("POST /api/alerts/501/feedback")).toBe(1);
  api.expectClean(["GET /api/"]);
});
