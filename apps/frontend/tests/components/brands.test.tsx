import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrandsClient } from "@/app/brands/brands-client";
import { BrandDetailClient } from "@/app/brands/[brandId]/brand-detail-client";

const jobs = vi.hoisted(() => ({ submitUrlJob: vi.fn() }));
vi.mock("@/lib/api/client", () => jobs);

const brand = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Örnek marka", status: "DRAFT", class_count: 2, dataset_count: 3,
  updated_at: "2026-09-16T00:00:00Z",
};

describe("brand list", () => {
  beforeEach(() => { vi.stubGlobal("fetch", vi.fn()); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("loads brands and links to the matching detail page", async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => [brand] } as Response);
    render(<BrandsClient />);
    const link = await screen.findByRole("link", { name: brand.name });
    expect(link).toHaveAttribute("href", `/brands/${brand.id}`);
    expect(fetch).toHaveBeenCalledWith("/api/v1/brands?limit=100&offset=0", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(screen.getByText("2")).toBeVisible();
    expect(screen.getByText("3")).toBeVisible();
  });

  it("blocks duplicate create requests and sanitizes API errors", async () => {
    let rejectCreate: ((value: Response) => void) | undefined;
    vi.mocked(fetch)
      .mockResolvedValueOnce({ ok: true, json: async () => [] } as Response)
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { rejectCreate = resolve; }));
    const user = userEvent.setup();
    render(<BrandsClient />);
    await user.type(screen.getByPlaceholderText("Yeni marka adı"), "Deneme");
    await user.click(screen.getByRole("button", { name: /Marka oluştur/ }));
    await user.click(screen.getByRole("button", { name: /Marka oluştur/ }));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenLastCalledWith("/api/v1/brands", expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "Deneme" }) }));
    rejectCreate?.({ ok: false, json: async () => ({ detail: "internal storage secret" }) } as Response);
    expect(await screen.findByRole("alert")).toHaveTextContent("Marka oluşturulamadı.");
    expect(document.body.innerHTML).not.toContain("internal storage secret");
  });

  it("ignores a pending load after unmount", async () => {
    let resolveLoad: ((value: Response) => void) | undefined;
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>((resolve) => { resolveLoad = resolve; }));
    const view = render(<BrandsClient />);
    view.unmount();
    resolveLoad?.({ ok: true, json: async () => [brand] } as Response);
    await waitFor(() => expect(document.body).not.toHaveTextContent(brand.name));
  });
});

describe("brand detail", () => {
  beforeEach(() => { vi.stubGlobal("fetch", vi.fn()); jobs.submitUrlJob.mockReset(); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("keeps a previous brand response out of the new brand state", async () => {
    let resolveOld: ((value: Response) => void) | undefined;
    vi.mocked(fetch)
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ ok: true, json: async () => ({
        ...brand, id: "22222222-2222-4222-8222-222222222222", name: "Yeni marka",
        created_at: brand.updated_at, classes: [], datasets: [],
      }) } as Response);
    const view = render(<BrandDetailClient brandId={brand.id} />);
    view.rerender(<BrandDetailClient brandId="22222222-2222-4222-8222-222222222222" />);
    expect(await screen.findByRole("heading", { name: "Yeni marka" })).toBeVisible();
    resolveOld?.({ ok: true, json: async () => ({ ...brand, created_at: brand.updated_at, classes: [], datasets: [] }) } as Response);
    await waitFor(() => expect(screen.queryByRole("heading", { name: brand.name })).not.toBeInTheDocument());
  });

  it("does not show internal API errors", async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: false, json: async () => ({ detail: "internal secret" }) } as Response);
    render(<BrandDetailClient brandId={brand.id} />);
    expect(await screen.findByText("İşlem tamamlanamadı.")).toBeVisible();
    expect(document.body.innerHTML).not.toContain("internal secret");
  });

  it("discards a previous brand's class mutation and keeps same-brand duplicate locked", async () => {
    const next = { ...brand, id: "22222222-2222-4222-8222-222222222222", name: "Yeni marka" };
    let finishClass!: (value: Response) => void;
    vi.mocked(fetch).mockImplementation((url) => {
      if (String(url).endsWith("/classes")) return new Promise<Response>((resolve) => { finishClass = resolve; });
      const selected = String(url).includes(next.id) ? next : brand;
      return Promise.resolve({ ok: true, json: async () => ({ ...selected, created_at: brand.updated_at, classes: [], datasets: [] }) } as Response);
    });
    const user = userEvent.setup();
    const view = render(<BrandDetailClient brandId={brand.id} />);
    await screen.findByRole("heading", { name: brand.name });
    await user.type(screen.getByPlaceholderText("Örn. wordmark, symbol, swoosh"), "Logo");
    const button = screen.getByRole("button", { name: /Sınıf ekle/ });
    await act(async () => { button.click(); button.click(); });
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/classes"))).toHaveLength(1);
    view.rerender(<BrandDetailClient brandId={next.id} />);
    await screen.findByRole("heading", { name: next.name });
    await act(async () => finishClass({ ok: true, json: async () => ({ id: "new-class", name: "Logo", color: "#FFFFFF" }) } as Response));
    expect(screen.queryByText("Logo")).not.toBeInTheDocument();
    view.unmount();
  });

  it("discards a previous brand's dataset mutation and pending unmount result", async () => {
    const next = { ...brand, id: "22222222-2222-4222-8222-222222222222", name: "Yeni marka" };
    let finishDataset!: (value: Response) => void;
    jobs.submitUrlJob.mockResolvedValue({ job_id: "33333333-3333-4333-8333-333333333333" });
    vi.mocked(fetch).mockImplementation((url) => {
      if (String(url).endsWith("/datasets")) return new Promise<Response>((resolve) => { finishDataset = resolve; });
      const selected = String(url).includes(next.id) ? next : brand;
      return Promise.resolve({ ok: true, json: async () => ({ ...selected, created_at: brand.updated_at, classes: [], datasets: [] }) } as Response);
    });
    const user = userEvent.setup();
    const view = render(<BrandDetailClient brandId={brand.id} />);
    await screen.findByRole("heading", { name: brand.name });
    await user.click(screen.getByRole("button", { name: /Videodan Kare Ekle/ }));
    await user.click(screen.getByRole("button", { name: /URL'den İçe Aktar/ }));
    await user.type(screen.getByLabelText(/Video URL/), "https://example.com/video.mp4");
    await user.click(screen.getByRole("button", { name: "＋ Kare Ekle" }));
    await waitFor(() => expect(finishDataset).toBeDefined());
    view.rerender(<BrandDetailClient brandId={next.id} />);
    await screen.findByRole("heading", { name: next.name });
    await act(async () => finishDataset({ ok: true, json: async () => ({ id: "old-dataset", name: "Old source", job_id: "33333333-3333-4333-8333-333333333333", annotation_project_id: null }) } as Response));
    expect(screen.queryByText("Old source")).not.toBeInTheDocument();
    view.unmount();
  });

  it("applies a current class result and discards one resolved after unmount", async () => {
    let finishSecond!: (value: Response) => void;
    let requests = 0;
    vi.mocked(fetch).mockImplementation((url) => {
      if (String(url).endsWith("/classes")) {
        requests += 1;
        if (requests === 1) return Promise.resolve({ ok: true, json: async () => ({ id: "first", name: "First", color: "#FFFFFF" }) } as Response);
        return new Promise<Response>((resolve) => { finishSecond = resolve; });
      }
      return Promise.resolve({ ok: true, json: async () => ({ ...brand, created_at: brand.updated_at, classes: [], datasets: [] }) } as Response);
    });
    const user = userEvent.setup();
    const view = render(<BrandDetailClient brandId={brand.id} />);
    await screen.findByRole("heading", { name: brand.name });
    await user.type(screen.getByPlaceholderText("Örn. wordmark, symbol, swoosh"), "First");
    await user.click(screen.getByRole("button", { name: /Sınıf ekle/ }));
    expect(await screen.findByText("First")).toBeVisible();
    await user.type(screen.getByPlaceholderText("Örn. wordmark, symbol, swoosh"), "Second");
    await user.click(screen.getByRole("button", { name: /Sınıf ekle/ }));
    view.unmount();
    await act(async () => finishSecond({ ok: true, json: async () => ({ id: "second", name: "Second", color: "#FFFFFF" }) } as Response));
    expect(screen.queryByText("Second")).not.toBeInTheDocument();
  });
});
