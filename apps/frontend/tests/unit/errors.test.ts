import { describe, expect, it } from "vitest";
import { failureMessage, safeApiError, userErrorMessage } from "@/lib/api/errors";
import { getJobStatus } from "@/lib/api/client";

describe("safe error messages", () => {
  it.each([
    [404, { detail: { code: "JOB_NOT_FOUND", message: "internal" } }, "İş bulunamadı."],
    [413, { detail: "Uploaded video is too large" }, "Video izin verilen boyuttan büyük."],
    [415, { detail: "Unsupported video upload type" }, "Bu video biçimi desteklenmiyor."],
    [422, { detail: [] }, "Lütfen form alanlarını kontrol edin."],
    [429, { detail: { code: "RATE_LIMITED", message: "internal" } }, "Çok fazla istek gönderildi."],
    [503, { detail: "http://minio:9000/private-key" }, "Hizmete şu anda ulaşılamıyor. Lütfen yeniden deneyin."],
  ])("maps HTTP %s without exposing detail", (status, payload, expected) => {
    expect(userErrorMessage(safeApiError(status, payload))).toBe(expected);
  });

  it("shows a bounded retry delay for rate limits", () => {
    expect(userErrorMessage(safeApiError(429, { detail: { code: "RATE_LIMITED", message: "internal" } }, "7"))).toBe(
      "Çok fazla istek gönderildi. 7 saniye bekleyip tekrar deneyin.",
    );
  });

  it("maps worker failures without returning backend messages", () => {
    expect(failureMessage("DOWNLOAD_TIMEOUT")).toContain("zaman aşımına");
    expect(failureMessage("UNKNOWN_INTERNAL_FAILURE")).toBe("İşleme tamamlanamadı.");
  });

  it("turns a malformed successful job response into a safe API error", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => new Response(JSON.stringify({ id: "job-1", status: "RUNNING", source_type: "URL", source: "x", created_at: "not-a-date" }), { status: 200 });
    await expect(getJobStatus("job-1")).rejects.toMatchObject({ status: 502 });
    globalThis.fetch = originalFetch;
  });
});
