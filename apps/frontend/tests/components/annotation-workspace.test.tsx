import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/errors";

const api = vi.hoisted(() => ({
  getOrCreateAnnotationProject: vi.fn(), getImageAnnotations: vi.fn(), putImageAnnotations: vi.fn(),
  createAnnotationClass: vi.fn(), updateAnnotationClass: vi.fn(), deleteAnnotationClass: vi.fn(),
}));
vi.mock("@/lib/api/client", () => api);
import { AnnotationWorkspace } from "@/components/annotations/annotation-workspace";

const jobId = "11111111-1111-4111-8111-111111111111";
const classId = "22222222-2222-4222-8222-222222222222";
const project = {
  id: "33333333-3333-4333-8333-333333333333", job_id: jobId, revision: 2,
  classes: [{ id: classId, yolo_index: 0, name: "Araç", color: "#ff0000" }],
  images: [
    { index: 4, filename: "one.jpg", completed: true, box_count: 1, preview_url: `/api/v1/jobs/${jobId}/annotations/images/4/preview` },
    { index: 9, filename: "two.jpg", completed: false, box_count: 0, preview_url: `/api/v1/jobs/${jobId}/annotations/images/9/preview` },
  ], page: 1, page_size: 100, total_images: 2,
  limits: { max_classes: 100, max_boxes_per_image: 200, max_boxes_per_project: 50_000 },
};
const existing = { id: "44444444-4444-4444-8444-444444444444", class_id: classId, x_center: .3, y_center: .3, width: .2, height: .2 };
async function readyCanvas(width = 640, height = 640) {
  const image = await screen.findByRole("img");
  Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } });
  fireEvent.load(image);
  return screen.queryByLabelText("Bounding box çalışma alanı") as SVGSVGElement | null;
}

describe("annotation workspace", () => {
  beforeEach(() => {
    Object.values(api).forEach((mock) => mock.mockReset());
    api.getOrCreateAnnotationProject.mockResolvedValue(project);
    api.getImageAnnotations.mockImplementation((_job, index) => Promise.resolve({ project_revision: 2, image_index: index, completed: index === 4, boxes: index === 4 ? [existing] : [] }));
    api.putImageAnnotations.mockImplementation((_job, index, _revision, completed, boxes) => Promise.resolve({ project_revision: 3, image_index: index, completed, boxes }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "prompt").mockReturnValue("Taşıt");
  });

  it("renders existing data, draws a bounded box and saves once", async () => {
    render(<AnnotationWorkspace jobId={jobId} />);
    expect(await screen.findByText("one.jpg")).toBeVisible();
    expect(screen.getByText("1 / 2 · 1 görsel etiketli")).toBeVisible();
    const canvas = await readyCanvas() as SVGSVGElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 10, top: 20, width: 500, height: 500, right: 510, bottom: 520, x: 10, y: 20, toJSON: () => ({}) });
    fireEvent.pointerDown(canvas, { pointerId: 1, button: 0, clientX: 410, clientY: 420 });
    fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 510, clientY: 520 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    const save = screen.getByRole("button", { name: "Kaydet" });
    fireEvent.click(save); fireEvent.click(save);
    await waitFor(() => expect(api.putImageAnnotations).toHaveBeenCalledTimes(1));
    const payload = api.putImageAnnotations.mock.calls[0][4];
    expect(payload).toHaveLength(2);
    expect(payload[1]).toMatchObject({ x_center: .9, y_center: .9, width: .2, height: .2 });
  });

  it("blocks drawing without an active class and guards dirty navigation", async () => {
    api.getOrCreateAnnotationProject.mockResolvedValue({ ...project, classes: [] });
    api.getImageAnnotations.mockResolvedValue({ project_revision: 2, image_index: 4, completed: false, boxes: [] });
    render(<AnnotationWorkspace jobId={jobId} />);
    const canvas = await readyCanvas() as SVGSVGElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(canvas, { pointerId: 1, button: 0, clientX: 10, clientY: 10 });
    expect(screen.getByRole("alert")).toHaveTextContent("aktif bir sınıf seçin");
    expect(api.putImageAnnotations).not.toHaveBeenCalled();
  });

  it("supports class create, rename and safe delete", async () => {
    const user = userEvent.setup();
    api.createAnnotationClass.mockResolvedValue({ revision: 3, annotation_class: { id: "55555555-5555-4555-8555-555555555555", yolo_index: 1, name: "İnsan", color: "#1c6b50" } });
    api.updateAnnotationClass.mockResolvedValue({ revision: 3, annotation_class: { ...project.classes[0], name: "Taşıt" } });
    api.deleteAnnotationClass.mockResolvedValue(4);
    render(<AnnotationWorkspace jobId={jobId} />);
    await readyCanvas();
    await user.type(await screen.findByLabelText("Yeni sınıf adı"), "İnsan");
    await user.click(screen.getByRole("button", { name: "Sınıf ekle" }));
    expect(api.createAnnotationClass).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Araç sınıfını yeniden adlandır" }));
    expect(api.updateAnnotationClass).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "İnsan sınıfını sil" }));
    expect(api.deleteAnnotationClass).toHaveBeenCalledOnce();
  });

  it("moves, resizes and deletes a selected box while protecting form input", async () => {
    render(<AnnotationWorkspace jobId={jobId} />);
    const canvas = await readyCanvas() as SVGSVGElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    let rect = canvas.querySelector("rect[data-box-id]")!;
    fireEvent.pointerDown(rect, { pointerId: 2, button: 0, clientX: 30, clientY: 30 });
    fireEvent.pointerMove(canvas, { pointerId: 2, clientX: 50, clientY: 50 }); fireEvent.pointerUp(canvas, { pointerId: 2 });
    rect = canvas.querySelector("rect[data-box-id]:not([data-handle])")!;
    expect(Number(rect.getAttribute("x"))).toBeCloseTo(.4);
    const handle = canvas.querySelector("rect[data-handle]")!;
    fireEvent.pointerDown(handle, { pointerId: 3, button: 0, clientX: 60, clientY: 60 });
    fireEvent.pointerMove(canvas, { pointerId: 3, clientX: 90, clientY: 90 }); fireEvent.pointerUp(canvas, { pointerId: 3 });
    expect(Number(rect.getAttribute("width"))).toBeCloseTo(.5);
    const input = screen.getByLabelText("Yeni sınıf adı"); input.focus(); fireEvent.keyDown(input, { key: "Backspace" });
    expect(canvas.querySelectorAll("rect[data-box-id]:not([data-handle])")).toHaveLength(1);
    canvas.focus(); fireEvent.keyDown(window, { key: "Delete" });
    expect(canvas.querySelectorAll("rect[data-box-id]:not([data-handle])")).toHaveLength(0);
  });

  it("blocks navigation while dirty and cleans a cancelled tiny drawing", async () => {
    render(<AnnotationWorkspace jobId={jobId} />);
    let canvas = await readyCanvas() as SVGSVGElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(canvas, { pointerId: 1, button: 0, clientX: 70, clientY: 70 }); fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 90, clientY: 90 }); fireEvent.pointerUp(canvas, { pointerId: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Sonraki görsel" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Kaydedilmemiş değişiklikler"); expect(screen.getByText("one.jpg")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Değişiklikleri geri al" }));
    await waitFor(() => expect(screen.getByText("1 kutu")).toBeVisible());
    canvas = await readyCanvas() as SVGSVGElement;
    fireEvent.pointerDown(canvas, { pointerId: 4, button: 0, clientX: 80, clientY: 80 }); fireEvent.pointerMove(canvas, { pointerId: 4, clientX: 80.2, clientY: 80.2 }); fireEvent.pointerCancel(canvas, { pointerId: 4 });
    expect(canvas.querySelectorAll("rect[data-box-id]:not([data-handle])")).toHaveLength(1);
  });

  it("deduplicates project creation in Strict Mode and shows safe rate-limit errors", async () => {
    const view = render(<StrictMode><AnnotationWorkspace jobId={jobId} /></StrictMode>);
    expect(await screen.findByText("one.jpg")).toBeVisible();
    expect(api.getOrCreateAnnotationProject).toHaveBeenCalledTimes(1);
    view.unmount();
    api.getOrCreateAnnotationProject.mockRejectedValue(new ApiError(429, "RATE_LIMITED", 7));
    render(<AnnotationWorkspace jobId={jobId} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("7 saniye");
    expect(screen.queryByText(/internal|bucket|object_key/i)).not.toBeInTheDocument();
  });

  it("shows a safe revision conflict and reload option", async () => {
    api.putImageAnnotations.mockRejectedValue(new ApiError(409, "ANNOTATION_REVISION_CONFLICT"));
    render(<AnnotationWorkspace jobId={jobId} />);
    const canvas = await readyCanvas() as SVGSVGElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(canvas, { pointerId: 1, button: 0, clientX: 50, clientY: 50 });
    fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 70, clientY: 70 });
    fireEvent.pointerUp(canvas, { pointerId: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Kaydet" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Verileriniz kaydedilmedi");
    expect(screen.getByRole("button", { name: "Sunucudaki sürümü yeniden yükle" })).toBeVisible();
    expect(screen.queryByText(/bucket|object key|internal/i)).not.toBeInTheDocument();
  });

  it("keeps the current image and boxes when the next image request fails", async () => {
    api.getImageAnnotations.mockImplementation((_job, index) => index === 9
      ? Promise.reject(new ApiError(503, "STORAGE_UNAVAILABLE"))
      : Promise.resolve({ project_revision: 2, image_index: 4, completed: true, boxes: [existing] }));
    render(<AnnotationWorkspace jobId={jobId} />);
    await screen.findByText("one.jpg");
    await readyCanvas();
    fireEvent.click(screen.getByRole("button", { name: /Sonraki/ }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.getByText("one.jpg")).toBeVisible();
    expect(screen.getByText("1 / 2 · 1 görsel etiketli")).toBeVisible();
    expect(screen.getByLabelText("Bounding box çalışma alanı").querySelectorAll("rect[data-box-id]:not([data-handle])")).toHaveLength(1);
  });

  it("locks every mutation after conflict and reloads the same image atomically", async () => {
    api.putImageAnnotations.mockRejectedValueOnce(new ApiError(409, "ANNOTATION_REVISION_CONFLICT"));
    render(<AnnotationWorkspace jobId={jobId} />);
    const canvas = await readyCanvas() as SVGSVGElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(canvas, { pointerId: 8, button: 0, clientX: 60, clientY: 60 }); fireEvent.pointerMove(canvas, { pointerId: 8, clientX: 80, clientY: 80 }); fireEvent.pointerUp(canvas, { pointerId: 8 });
    fireEvent.click(screen.getByRole("button", { name: "Kaydet" }));
    const reload = await screen.findByRole("button", { name: /Sunucudaki sürümü/ });
    expect(screen.getByRole("button", { name: "Kaydet" })).toBeDisabled();
    fireEvent.pointerDown(canvas, { pointerId: 9, button: 0, clientX: 10, clientY: 10 });
    expect(canvas.querySelectorAll("rect[data-box-id]:not([data-handle])")).toHaveLength(2);
    fireEvent.click(reload);
    await waitFor(() => expect(api.getOrCreateAnnotationProject).toHaveBeenCalledTimes(2));
    const reloadedCanvas = await readyCanvas() as SVGSVGElement;
    await waitFor(() => expect(reloadedCanvas.querySelectorAll("rect[data-box-id]:not([data-handle])")).toHaveLength(1));
    expect(api.getImageAnnotations).toHaveBeenLastCalledWith(jobId, 4, expect.any(AbortSignal));
  });

  it("renders four labelled corner handles and finalizes lost pointer capture once", async () => {
    render(<AnnotationWorkspace jobId={jobId} />);
    const canvas = await readyCanvas() as SVGSVGElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(canvas.querySelector("rect[data-box-id]")!, { pointerId: 3, button: 0, clientX: 30, clientY: 30 });
    expect(canvas.querySelectorAll("rect[data-handle]")).toHaveLength(4);
    expect(screen.getByRole("button", { name: "Sol üst boyutlandırma tutamacı; ok tuşlarıyla yeniden boyutlandır" })).toBeVisible();
    fireEvent.pointerDown(canvas, { pointerId: 10, button: 0, clientX: 90, clientY: 90 }); fireEvent.pointerMove(canvas, { pointerId: 10, clientX: 90.2, clientY: 90.2 });
    fireEvent.lostPointerCapture(canvas, { pointerId: 10 }); fireEvent.pointerUp(canvas, { pointerId: 10 });
    expect(canvas.querySelectorAll("rect[data-box-id]:not([data-handle])")).toHaveLength(1);
  });

  it("guards gallery navigation, beforeunload and browser back while dirty", async () => {
    vi.spyOn(window.history, "forward").mockImplementation(() => undefined);
    render(<AnnotationWorkspace jobId={jobId} />);
    const canvas = await readyCanvas() as SVGSVGElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(canvas, { pointerId: 11, button: 0, clientX: 60, clientY: 60 }); fireEvent.pointerMove(canvas, { pointerId: 11, clientX: 80, clientY: 80 }); fireEvent.pointerUp(canvas, { pointerId: 11 });
    vi.mocked(window.confirm).mockReturnValue(false);
    const link = screen.getByRole("link", { name: /Galeriye dön/ });
    expect(fireEvent.click(link)).toBe(false);
    const unload = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(unload); expect(unload.defaultPrevented).toBe(true);
    window.dispatchEvent(new PopStateEvent("popstate")); expect(window.history.forward).toHaveBeenCalledOnce();
  });

  it("aborts project and mutation requests on unmount", async () => {
    let projectSignal: AbortSignal | undefined;
    api.getOrCreateAnnotationProject.mockImplementation((_job, signal) => { projectSignal = signal; return new Promise(() => undefined); });
    const view = render(<AnnotationWorkspace jobId={jobId} />);
    await waitFor(() => expect(projectSignal).toBeDefined()); view.unmount();
    expect(projectSignal?.aborted).toBe(true);
  });

  it.each([[1280, 720], [640, 639], [639, 640]])("fails closed for an invalid %sx%s preview", async (width, height) => {
    render(<AnnotationWorkspace jobId={jobId} />);
    expect(await readyCanvas(width, height)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("640×640");
    expect(screen.getByRole("button", { name: "Kaydet" })).toBeDisabled();
  });

  it("keeps the overlay locked before load and after an image error", async () => {
    render(<AnnotationWorkspace jobId={jobId} />);
    const image = await screen.findByRole("img");
    expect(screen.queryByLabelText("Bounding box çalışma alanı")).not.toBeInTheDocument();
    fireEvent.error(image);
    expect(screen.getByRole("alert")).toHaveTextContent("önizlemesi yüklenemedi");
    expect(screen.queryByLabelText("Bounding box çalışma alanı")).not.toBeInTheDocument();
    expect(api.putImageAnnotations).not.toHaveBeenCalled();
  });

  it("ignores an old image load after navigating to a new preview", async () => {
    render(<AnnotationWorkspace jobId={jobId} />);
    const oldImage = await screen.findByRole("img");
    Object.defineProperties(oldImage, { naturalWidth: { configurable: true, value: 640 }, naturalHeight: { configurable: true, value: 640 } });
    fireEvent.load(oldImage);
    fireEvent.click(screen.getByRole("button", { name: "Sonraki görsel" }));
    await screen.findByText("two.jpg");
    fireEvent.load(oldImage);
    expect(screen.queryByLabelText("Bounding box çalışma alanı")).not.toBeInTheDocument();
  });

  it("supports bounded keyboard resizing from every corner and saves the result", async () => {
    render(<AnnotationWorkspace jobId={jobId} />);
    const canvas = await readyCanvas() as SVGSVGElement;
    fireEvent.focus(canvas.querySelector("rect[data-box-id]")!);
    const handles = [...canvas.querySelectorAll<SVGRectElement>("rect[data-handle]")];
    expect(handles).toHaveLength(4);
    for (const handle of handles) expect(handle).toHaveAttribute("tabindex", "0");
    fireEvent.keyDown(canvas.querySelector('[data-handle="nw"]')!, { key: "ArrowLeft" });
    fireEvent.keyDown(canvas.querySelector('[data-handle="nw"]')!, { key: "ArrowUp" });
    fireEvent.keyDown(canvas.querySelector('[data-handle="ne"]')!, { key: "ArrowRight" });
    fireEvent.keyDown(canvas.querySelector('[data-handle="ne"]')!, { key: "ArrowUp" });
    fireEvent.keyDown(canvas.querySelector('[data-handle="sw"]')!, { key: "ArrowLeft" });
    fireEvent.keyDown(canvas.querySelector('[data-handle="sw"]')!, { key: "ArrowDown" });
    fireEvent.keyDown(canvas.querySelector('[data-handle="se"]')!, { key: "ArrowRight", shiftKey: true });
    fireEvent.keyDown(canvas.querySelector('[data-handle="se"]')!, { key: "ArrowDown", shiftKey: true });
    fireEvent.click(screen.getByRole("button", { name: "Kaydet" }));
    await waitFor(() => expect(api.putImageAnnotations).toHaveBeenCalledOnce());
    const saved = api.putImageAnnotations.mock.calls[0][4][0];
    expect(saved.width).toBeGreaterThan(.2); expect(saved.height).toBeGreaterThan(.2);
    expect(saved.x_center - saved.width / 2).toBeGreaterThanOrEqual(0);
    expect(saved.x_center + saved.width / 2).toBeLessThanOrEqual(1);
  });
});
