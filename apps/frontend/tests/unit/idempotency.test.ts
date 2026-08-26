import { beforeEach, describe, expect, it, vi } from "vitest";
import { idempotencyKeyFor, uploadSignature, urlSignature } from "@/lib/idempotency";

describe("submission idempotency", () => {
  beforeEach(() => vi.stubGlobal("crypto", { randomUUID: vi.fn().mockReturnValueOnce("one").mockReturnValueOnce("two") }));

  it("keeps the key for a retry and rotates it for changed content", () => {
    const first = idempotencyKeyFor(urlSignature("https://example.com/a", 5, 1), null);
    const retry = idempotencyKeyFor(urlSignature("https://example.com/a", 5, 1), first);
    const changed = idempotencyKeyFor(urlSignature("https://example.com/b", 5, 1), retry);
    expect(first.key).toBe("web-one");
    expect(retry).toBe(first);
    expect(changed.key).toBe("web-two");
  });

  it("includes file identity and processing values in upload signatures", () => {
    const file = new File(["video"], "clip.mp4", { type: "video/mp4", lastModified: 7 });
    expect(uploadSignature(file, 5, 1)).not.toBe(uploadSignature(file, 6, 1));
  });
});
