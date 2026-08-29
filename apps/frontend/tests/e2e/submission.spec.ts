import { expect, test } from "@playwright/test";

test("submits a URL through same-origin API and reaches success", async ({ page }) => {
  const requests: string[] = [];
  let polls = 0;
  await page.route("**/api/v1/jobs/url", async (route) => {
    requests.push(route.request().url());
    expect(route.request().headers()["idempotency-key"]).toMatch(/^web-/);
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ job_id: "11111111-1111-4111-8111-111111111111", status: "PENDING_DISPATCH", status_url: "/api/v1/jobs/11111111-1111-4111-8111-111111111111" }) });
  });
  await page.route("**/api/v1/jobs/11111111-1111-4111-8111-111111111111", async (route) => {
    requests.push(route.request().url()); polls += 1;
    const status = polls === 1 ? "RUNNING" : "SUCCEEDED";
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "11111111-1111-4111-8111-111111111111", status, source_type: "URL", source: "https://example.com/video", created_at: "2026-08-24T10:00:00Z", started_at: null, completed_at: null, failure: null, result: status === "SUCCEEDED" ? { available: true, metadata_url: "/api/v1/jobs/1/result", manifest_download_url: "/api/v1/jobs/1/result/manifest" } : null }) });
  });
  await page.goto("/");
  const expectedOrigin = new URL(page.url()).origin;
  await page.getByRole("tab", { name: "Video URL’si" }).click();
  await page.getByLabel("Video bağlantısı").fill("https://example.com/video");
  await page.getByRole("button", { name: "İşi başlat" }).click();
  await expect(page).toHaveURL(/\/jobs\/11111111/);
  await expect(page.getByRole("heading", { name: "İşleme tamamlandı" })).toBeVisible({ timeout: 8_000 });
  expect(requests.every((url) => new URL(url).origin === expectedOrigin)).toBe(true);
  expect(requests.join(" ")).not.toContain("backend:8000");
  expect(requests.join(" ")).not.toContain("minio");
});

test("shows a safe 404 message without logging internal detail", async ({ page }) => {
  const messages: string[] = [];
  page.on("console", (message) => messages.push(message.text()));
  await page.route("**/api/v1/jobs/missing", (route) => route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: { code: "JOB_NOT_FOUND", message: "s3://private/key credential" } }) }));
  await page.goto("/jobs/missing");
  await expect(page.locator(".status-card").getByRole("alert")).toHaveText("İş bulunamadı.");
  expect(messages.join(" ")).not.toContain("s3://");
  expect(messages.join(" ")).not.toContain("credential");
});
