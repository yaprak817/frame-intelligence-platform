import { afterEach, describe, expect, it, vi } from "vitest";
import { canonicalUuid, createFrameAccess, getJobResult, getManifestDownload, isFrameAccess, isJobStatus, isResultManifest } from "@/lib/api/client";
import { safeApiError, userErrorMessage } from "@/lib/api/errors";

const hash = "a".repeat(64);
const jobId = "11111111-1111-4111-8111-111111111111";
const manifest = { schema_version: 1, job_id: jobId, created_at: "2026-08-24T10:00:00Z", summary: { frames_saved: 1, candidates: 4, shortlisted: 2, duplicates_removed: 1, processing_seconds: 2.5, duration_seconds: 8 }, frames: [{ index: 0, filename: "frame_000000_1000ms_640x480.jpg", content_type: "image/jpeg", size_bytes: 123, sha256: hash, timestamp_ms: 1000, width: 640, height: 480, access_url: `/api/v1/jobs/${jobId}/result/frames/0/access` }] };
const access = { url: "https://downloads.example/frame.jpg?signature=secret", expires_at: "2026-08-24T10:05:00Z", content_type: "image/jpeg", size_bytes: 123, sha256: hash };
const secondFrame = { ...manifest.frames[0], index: 1, filename: "frame_000001_2000ms_640x480.jpg", timestamp_ms: 2000, access_url: `/api/v1/jobs/${jobId}/result/frames/1/access` };
const twoFrameManifest = { ...manifest, summary: { ...manifest.summary, frames_saved: 2 }, frames: [manifest.frames[0], secondFrame] };

describe("result API runtime guards", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("accepts only the five backend job states", () => {
    const base = { id: jobId, source_type: "URL", source: "https://example.com/video", created_at: "2026-08-24T10:00:00Z", started_at: null, completed_at: null, failure: null, result: null };
    for (const status of ["PENDING_DISPATCH", "QUEUED", "RUNNING"]) expect(isJobStatus({ ...base, status })).toBe(true);
    expect(isJobStatus({ ...base, status: "CANCELLED" })).toBe(false);
    expect(isJobStatus({ ...base, status: "FAILED", failure: { code: "INVALID_VIDEO", message: "detail" } })).toBe(true);
    expect(isJobStatus({ ...base, status: "FAILED", failure: null })).toBe(true);
    expect(isJobStatus({ ...base, status: "SUCCEEDED", result: { available: true, metadata_url: `/api/v1/jobs/${jobId}/result`, manifest_download_url: `/api/v1/jobs/${jobId}/result/manifest` } })).toBe(true);
  });
  it("canonicalizes equivalent UUID spellings without equating different or invalid UUIDs", () => {
    expect(canonicalUuid(jobId.toUpperCase())).toBe(jobId);
    expect(canonicalUuid(jobId.replaceAll("-", ""))).toBe(jobId);
    expect(canonicalUuid(`{${jobId}}`)).toBe(jobId);
    expect(canonicalUuid("22222222-2222-4222-8222-222222222222")).not.toBe(jobId);
    expect(canonicalUuid("not-a-uuid")).toBeNull();
  });
  it("rejects internal or malformed manifest fields", () => {
    expect(isResultManifest(manifest)).toBe(true);
    expect(isResultManifest({ ...manifest, run_token: "secret" })).toBe(false);
    expect(isResultManifest({ ...manifest, frames: [{ ...manifest.frames[0], object_key: "private/key" }] })).toBe(false);
    expect(isResultManifest({ ...manifest, frames: [{ ...manifest.frames[0], sha256: "bad" }] })).toBe(false);
  });
  it.each([
    ["NaN processing duration", { ...manifest, summary: { ...manifest.summary, processing_seconds: Number.NaN } }],
    ["positive Infinity duration", { ...manifest, summary: { ...manifest.summary, duration_seconds: Number.POSITIVE_INFINITY } }],
    ["negative Infinity duration", { ...manifest, summary: { ...manifest.summary, duration_seconds: Number.NEGATIVE_INFINITY } }],
    ["zero frame size", { ...manifest, frames: [{ ...manifest.frames[0], size_bytes: 0 }] }],
    ["zero frame width", { ...manifest, frames: [{ ...manifest.frames[0], width: 0, filename: "frame_000000_1000ms_0x480.jpg" }] }],
    ["zero frame height", { ...manifest, frames: [{ ...manifest.frames[0], height: 0, filename: "frame_000000_1000ms_640x0.jpg" }] }],
    ["wrong frame content type", { ...manifest, frames: [{ ...manifest.frames[0], content_type: "image/png" }] }],
    ["wrong frame access path", { ...manifest, frames: [{ ...manifest.frames[0], access_url: `/api/v1/jobs/${jobId}/result/frames/9/access` }] }],
    ["gapped multi-frame indexes", { ...twoFrameManifest, frames: [manifest.frames[0], { ...secondFrame, index: 2, filename: "frame_000002_2000ms_640x480.jpg", access_url: `/api/v1/jobs/${jobId}/result/frames/2/access` }] }],
    ["out-of-order multi-frame indexes", { ...twoFrameManifest, frames: [secondFrame, manifest.frames[0]] }],
    ["frame count mismatch", { ...manifest, summary: { ...manifest.summary, frames_saved: 2 } }],
    ["filename metadata mismatch", { ...manifest, frames: [{ ...manifest.frames[0], filename: "frame_000000_999ms_640x480.jpg" }] }],
  ])("rejects the %s manifest invariant", (_label, candidate) => expect(isResultManifest(candidate)).toBe(false));
  it("rejects unsafe and malformed access responses", () => {
    expect(isFrameAccess(access)).toBe(true);
    expect(isFrameAccess({ ...access, url: "javascript:alert(1)" })).toBe(false);
    expect(isFrameAccess({ ...access, object_key: "secret" })).toBe(false);
  });
  it("fetches result, frame access and public manifest through same-origin endpoints", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(manifest), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(access), { status: 200 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetcher);
    await expect(getJobResult(jobId.toUpperCase())).resolves.toEqual(manifest);
    await expect(createFrameAccess(jobId, 0)).resolves.toEqual(access);
    await expect(getManifestDownload(jobId)).resolves.toMatchObject({ size: 2, type: "application/json" });
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([`/api/v1/jobs/${jobId.toUpperCase()}/result`, `/api/v1/jobs/${jobId}/result/frames/0/access`, `/api/v1/jobs/${jobId}/result/manifest`]);
  });
  it.each([[404, "JOB_NOT_FOUND"], [409, "RESULT_NOT_READY"], [502, "MANIFEST_INVALID"], [503, "STORAGE_UNAVAILABLE"]])("maps %s safely", async (status, code) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code, message: "s3://secret/key" } }), { status })));
    await expect(getJobResult(jobId)).rejects.toMatchObject({ status, code });
  });
  it.each([
    [409, "RESULT_NOT_READY", "Sonuç henüz hazır değil."],
    [502, "RESULT_UNAVAILABLE", "Sonuçlara şu anda ulaşılamıyor."],
    [502, "MANIFEST_INVALID", "Sonuç verisi güvenli biçimde doğrulanamadı."],
    [404, "ARTIFACT_NOT_FOUND", "İstenen sonuç karesi bulunamadı."],
    [503, "STORAGE_UNAVAILABLE", "Dosya servisine şu anda ulaşılamıyor."],
  ])("maps public error %s/%s to a fixed safe message", (status, code, expected) => {
    const internal = "s3://private-bucket/jobs/private-object?credential=secret http://backend:8000";
    const message = userErrorMessage(safeApiError(status, { detail: { code, message: internal } }));
    expect(message).toBe(expected);
    for (const secret of ["private-bucket", "private-object", "credential", "secret", "backend:8000", internal]) expect(message).not.toContain(secret);
  });
});
