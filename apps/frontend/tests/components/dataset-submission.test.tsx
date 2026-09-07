import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { JobSubmission } from "@/components/submission/job-submission";

const { routerPush } = vi.hoisted(() => ({ routerPush: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPush }) }));

class DatasetXHR {
  static latest: DatasetXHR;
  uploadListeners: Record<string, (event: ProgressEvent) => void> = {};
  listeners: Record<string, () => void> = {};
  upload = { addEventListener: (name: string, callback: (event: ProgressEvent) => void) => { this.uploadListeners[name] = callback; } };
  open = vi.fn();
  setRequestHeader = vi.fn();
  send = vi.fn();
  abort = vi.fn();
  status = 202;
  response = { job_id: "11111111-1111-4111-8111-111111111111" };
  constructor() { DatasetXHR.latest = this; }
  addEventListener(name: string, callback: () => void) { this.listeners[name] = callback; }
  set responseType(_value: XMLHttpRequestResponseType) {}
}

describe("image dataset submission", () => {
  beforeEach(() => {
    routerPush.mockReset();
    vi.stubGlobal("crypto", { randomUUID: () => "dataset-request" });
    vi.stubGlobal("XMLHttpRequest", DatasetXHR);
  });

  it("selects multiple images, reports count and clears selection", async () => {
    const user = userEvent.setup();
    render(<JobSubmission />);
    await user.click(screen.getByRole("tab", { name: "Görsel veri seti" }));
    const input = screen.getByLabelText(/Görselleri seçin/);
    await user.upload(input, [
      new File(["a"], "a.jpg", { type: "image/jpeg" }),
      new File(["bb"], "b.png", { type: "image/png" }),
    ]);
    expect(input).toHaveValue("");
    expect(screen.getByText(/2 dosya/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Seçimi temizle" }));
    expect(screen.getByText(/0 dosya/)).toBeVisible();
  });

  it.each([
    ["Görselleri seçin", new File(["a"], "same.jpg", { type: "image/jpeg" })],
    ["Bir ZIP seçin", new File(["z"], "same.zip", { type: "application/zip" })],
  ])("allows the same %s file to be selected twice", async (label, selected) => {
    const user = userEvent.setup();
    render(<JobSubmission />);
    await user.click(screen.getByRole("tab", { name: "Görsel veri seti" }));
    if (selected.name.endsWith(".zip")) {
      await user.click(screen.getByRole("radio", { name: "ZIP arşivi" }));
    }
    const input = screen.getByLabelText(new RegExp(label));
    await user.upload(input, selected);
    expect(input).toHaveValue("");
    await user.upload(input, selected);
    expect(screen.getByText(/1 dosya/)).toBeVisible();
  });

  it("supports tab keyboard navigation and moves focus", async () => {
    const user = userEvent.setup();
    render(<JobSubmission />);
    const upload = screen.getByRole("tab", { name: "Dosya yükle" });
    upload.focus();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Görsel veri seti" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(upload).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Görsel veri seti" })).toHaveFocus();
  });

  it("guards progress and completion callbacks after unmount", async () => {
    const user = userEvent.setup();
    const view = render(<JobSubmission />);
    await user.click(screen.getByRole("tab", { name: "Görsel veri seti" }));
    await user.upload(
      screen.getByLabelText(/Görselleri seçin/),
      new File(["a"], "a.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "İşi başlat" }));
    expect(screen.getByLabelText("Görsel veri seti yükleme ilerlemesi")).toBeVisible();
    view.unmount();
    expect(() => {
      DatasetXHR.latest.uploadListeners.progress?.({ lengthComputable: true, loaded: 1, total: 1 } as ProgressEvent);
      DatasetXHR.latest.listeners.load?.();
      DatasetXHR.latest.listeners.error?.();
    }).not.toThrow();
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("keeps ZIP and image selection modes mutually exclusive", async () => {
    const user = userEvent.setup();
    render(<JobSubmission />);
    await user.click(screen.getByRole("tab", { name: "Görsel veri seti" }));
    await user.upload(
      screen.getByLabelText(/Görselleri seçin/),
      new File(["a"], "a.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("radio", { name: "ZIP arşivi" }));
    expect(screen.getByText(/0 dosya/)).toBeVisible();
    const zip = screen.getByLabelText(/Bir ZIP seçin/);
    expect(zip).not.toHaveAttribute("multiple");
    await user.upload(zip, new File(["zip"], "images.zip", { type: "application/zip" }));
    expect(screen.getByText(/1 dosya/)).toBeVisible();
  });
});
