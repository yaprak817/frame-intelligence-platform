import { expect, test, type Page } from "@playwright/test";

const jobId = "11111111-1111-4111-8111-111111111111";
const hash = "a".repeat(64);

async function mockResult(page: Page, frameCount = 1) {
  await page.route(`**/api/v1/jobs/${jobId}`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: jobId, status: "SUCCEEDED", source_type: "UPLOAD", source: "clip.mp4", created_at: "2026-08-24T10:00:00Z", started_at: "2026-08-24T10:00:01Z", completed_at: "2026-08-24T10:00:03Z", failure: null, result: { result_kind: "VIDEO_FRAMES", available: true, metadata_url: `/api/v1/jobs/${jobId}/result`, manifest_download_url: `/api/v1/jobs/${jobId}/result/manifest` } }) }));
  await page.route(`**/api/v1/jobs/${jobId}/result`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: 1, job_id: jobId, created_at: "2026-08-24T10:00:03Z", summary: { frames_saved: frameCount, candidates: frameCount, shortlisted: frameCount, duplicates_removed: 0, processing_seconds: 2, duration_seconds: 10 }, frames: Array.from({ length: frameCount }, (_, index) => ({ index, filename: `frame_${String(index).padStart(6, "0")}_${index * 1000}ms_640x480.jpg`, content_type: "image/jpeg", size_bytes: 4, sha256: hash, timestamp_ms: index * 1000, width: 640, height: 480, access_url: `/api/v1/jobs/${jobId}/result/frames/${index}/access` })) }) }));
  await page.route(`**/api/v1/jobs/${jobId}/result/frames/0/access`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ url: "https://frames.example/frame.jpg", expires_at: "2099-01-01T00:00:00Z", content_type: "image/jpeg", size_bytes: 4, sha256: hash }) }));
  await page.route("https://frames.example/frame.jpg", (route) => route.fulfill({ status: 200, contentType: "image/jpeg", body: Buffer.from([255, 216, 255, 217]) }));
  await page.route(`**/api/v1/jobs/${jobId}/result/manifest`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: "{}" }));
}

test("successful job opens gallery, frame modal and downloads public manifest", async ({ page }) => {
  await mockResult(page);
  await page.goto(`/jobs/${jobId}`);
  await page.getByRole("link", { name: "Sonuçları görüntüle" }).click();
  await expect(page.getByRole("heading", { name: "Analiz özeti" })).toBeVisible();
  const frame = page.getByRole("button", { name: /büyüt/ }); await frame.click();
  await expect(page.getByRole("dialog")).toBeVisible(); await page.keyboard.press("Escape"); await expect(page.getByRole("dialog")).toBeHidden(); await expect(frame).toBeFocused();
  const downloadPromise = page.waitForEvent("download"); await page.getByRole("button", { name: "Public manifesti indir" }).click(); const download = await downloadPromise; expect(download.suggestedFilename()).toBe(`job-${jobId}-manifest.json`);
});

test("result gallery remains usable on a mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockResult(page, 0);
  await page.goto(`/jobs/${jobId}/result`);
  await expect(page.getByRole("heading", { name: "Frame galerisi" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Gösterilecek kare yok" })).toBeVisible();
});
