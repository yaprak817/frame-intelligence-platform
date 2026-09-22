import { expect, test } from "@playwright/test";

const jobId = "11111111-1111-4111-8111-111111111111";
const projectId = "22222222-2222-4222-8222-222222222222";
const classId = "33333333-3333-4333-8333-333333333333";

test("video result opens the gallery-first annotation workspace and persists a box", async ({ page }) => {
  let revision = 1;
  let boxes: unknown[] = [];
  const requested: string[] = [];
  page.on("request", (request) => requested.push(request.url()));
  await page.route(`**/api/v1/jobs/${jobId}`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: jobId, status: "SUCCEEDED", source_type: "UPLOAD", source: "clip.mp4", created_at: "2026-09-11T00:00:00Z", started_at: null, completed_at: "2026-09-11T00:01:00Z", failure: null, result: { result_kind: "VIDEO_FRAMES", available: true, metadata_url: `/api/v1/jobs/${jobId}/result`, manifest_download_url: `/api/v1/jobs/${jobId}/result/manifest` } }) }));
  await page.route(`**/api/v1/jobs/${jobId}/result`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: 1, job_id: jobId, created_at: "2026-09-11T00:01:00Z", summary: { frames_saved: 2, candidates: 2, shortlisted: 2, duplicates_removed: 0, processing_seconds: 1, duration_seconds: 2 }, frames: [] }) }));
  await page.route(`**/api/v1/jobs/${jobId}/annotations?*`, async (route) => {
    const images = [0, 1].map((index) => ({ index, filename: `${index}.jpg`, width: 640, height: 360, timestamp_ms: index * 1000, completed: index === 0 && boxes.length > 0, box_count: index === 0 ? boxes.length : 0, preview_url: `/api/v1/jobs/${jobId}/annotations/images/${index}/preview` }));
    await route.fulfill({ status: route.request().method() === "POST" ? 201 : 200, contentType: "application/json", body: JSON.stringify({ id: projectId, job_id: jobId, revision, classes: [{ id: classId, yolo_index: 0, name: "Araç", color: "#ff0000" }], images, page: 1, page_size: 100, total_images: 2, limits: { max_classes: 100, max_boxes_per_image: 200, max_boxes_per_project: 50000 } }) });
  });
  await page.route(new RegExp(`/api/v1/jobs/${jobId}/annotations/images/(0|1)$`), async (route) => {
    const index = Number(route.request().url().split("/").at(-1));
    if (route.request().method() === "PUT") { const body = route.request().postDataJSON(); boxes = body.boxes; revision += 1; }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ project_revision: revision, image_index: index, completed: index === 0 && boxes.length > 0, boxes: index === 0 ? boxes : [] }) });
  });
  await page.route(`**/api/v1/jobs/${jobId}/annotations/images/*/preview`, (route) => route.fulfill({ status: 200, contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#ddd"/></svg>' }));

  await page.goto(`/jobs/${jobId}/result`);
  await page.getByRole("button", { name: "Etiketlemeye Ba\u015fla" }).click();
  await expect(page.getByRole("heading", { name: "Frame ve görsel galerisi" })).toBeVisible();
  await expect(page.getByRole("link", { name: /^0\.jpg/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /^1\.jpg/ })).toBeVisible();
  await expect(page.getByLabel("Bounding box çalışma alanı")).toHaveCount(0);
  await page.getByRole("link", { name: /^0\.jpg/ }).click();
  await expect(page).toHaveURL(new RegExp(`/jobs/${jobId}/annotations/0$`));
  const canvas = page.getByLabel("Bounding box çalışma alanı");
  await expect(canvas).toBeVisible();
  const bounds = await canvas.boundingBox();
  if (!bounds) throw new Error("annotation canvas has no bounds");
  await page.mouse.move(bounds.x + 100, bounds.y + 100); await page.mouse.down(); await page.mouse.move(bounds.x + 250, bounds.y + 250); await page.mouse.up();
  await page.getByRole("button", { name: "Kaydet ve sonraki" }).click();
  await expect(page.getByText("1.jpg")).toBeVisible();
  await page.reload();
  await expect(page.getByText("0.jpg")).toBeVisible();
  await expect(page.locator("rect[data-box-id]")).toHaveCount(1);
  expect(requested.every((url) => new URL(url).origin === new URL(page.url()).origin)).toBe(true);
  expect((await page.content())).not.toMatch(/backend:8000|minio:9000|run_token|object_key/i);
});
