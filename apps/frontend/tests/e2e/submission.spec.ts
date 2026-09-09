import { expect, test } from "@playwright/test";

test("submits a URL through same-origin API and reaches success", async ({ page }) => {
  const requests: string[] = [];
  const jobId = "11111111-1111-4111-8111-111111111111";
  let polls = 0;
  await page.route("**/api/v1/jobs/url", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(new URL(route.request().url()).pathname).toBe("/api/v1/jobs/url");
    requests.push(route.request().url());
    expect(route.request().headers()["idempotency-key"]).toMatch(/^web-/);
    await route.fulfill({ status: 202, json: { job_id: jobId, status: "PENDING_DISPATCH", status_url: `/api/v1/jobs/${jobId}` } });
  });
  await page.route(`**/api/v1/jobs/${jobId}`, async (route) => {
    expect(route.request().method()).toBe("GET");
    expect(new URL(route.request().url()).pathname).toBe(`/api/v1/jobs/${jobId}`);
    requests.push(route.request().url()); polls += 1;
    const status = polls === 1 ? "RUNNING" : "SUCCEEDED";
    await route.fulfill({ status: 200, json: { id: jobId, status, source_type: "URL", source: "https://example.com/video", created_at: "2026-08-24T10:00:00Z", started_at: "2026-08-24T10:00:01Z", completed_at: status === "SUCCEEDED" ? "2026-08-24T10:00:03Z" : null, failure: null, result: status === "SUCCEEDED" ? { result_kind: "VIDEO_FRAMES", available: true, metadata_url: `/api/v1/jobs/${jobId}/result`, manifest_download_url: `/api/v1/jobs/${jobId}/result/manifest` } : null } });
  });
  await page.goto("/");
  const expectedOrigin = new URL(page.url()).origin;
  await page.getByRole("tab", { name: "Video URL’si" }).click();
  const urlInput = page.getByLabel("Video bağlantısı");
  const submit = page.getByRole("button", { name: "İşi başlat" });
  await expect(urlInput).toBeEnabled();
  await expect(submit).toBeEnabled();
  await urlInput.fill("https://example.com/video");
  const [postRequest, postResponse] = await Promise.all([
    page.waitForRequest((request) => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/jobs/url"),
    page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/v1/jobs/url"),
    page.waitForURL(new RegExp(`/jobs/${jobId}$`)),
    submit.click(),
  ]);
  expect(postRequest.url()).toBe(`${expectedOrigin}/api/v1/jobs/url`);
  expect(postResponse.status()).toBe(202);
  expect(requests.filter((url) => new URL(url).pathname === "/api/v1/jobs/url")).toHaveLength(1);
  await expect(page).toHaveURL(new RegExp(`/jobs/${jobId}$`));
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
