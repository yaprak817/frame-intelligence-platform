import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/annotations/annotation-gallery", () => ({
  AnnotationGallery: ({ jobId }: { jobId: string }) => (
    <div data-testid="annotation-gallery">{jobId}</div>
  ),
}));

import { BrandAnnotationGallery } from "@/components/annotations/brand-annotation-gallery";

describe("brand annotation gallery", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the annotation project through the brand-scoped endpoint", async () => {
    const brandId = "11111111-1111-4111-8111-111111111111";
    const jobId = "22222222-2222-4222-8222-222222222222";

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ job_id: jobId }),
    });

    vi.stubGlobal("fetch", fetchMock);

    render(<BrandAnnotationGallery brandId={brandId} />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Marka etiketleme alan? haz?rlan?yor",
    );

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/v1/brands/${brandId}/annotations?page=1&page_size=100`,
        expect.objectContaining({
          method: "POST",
          cache: "no-store",
          signal: expect.any(AbortSignal),
        }),
      ),
    );

    expect(await screen.findByTestId("annotation-gallery")).toHaveTextContent(
      jobId,
    );
  });
});
