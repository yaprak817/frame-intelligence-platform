import { beforeEach, describe, expect, it, vi } from "vitest";
import { uploadVideo } from "@/lib/api/upload";

class ProgressXHR {
  static latest: ProgressXHR;
  upload = { addEventListener: (_name: string, callback: (event: ProgressEvent) => void) => { this.progress = callback; } };
  progress?: (event: ProgressEvent) => void;
  open() {}
  setRequestHeader() {}
  addEventListener() {}
  send() {}
  abort() {}
  set responseType(_value: XMLHttpRequestResponseType) {}
  constructor() { ProgressXHR.latest = this; }
}

describe("upload progress", () => {
  beforeEach(() => {
    vi.stubGlobal("XMLHttpRequest", ProgressXHR);
  });

  it.each([
    [{ lengthComputable: true, loaded: 1, total: 0 }, null],
    [{ lengthComputable: true, loaded: Number.NaN, total: 10 }, null],
    [{ lengthComputable: true, loaded: 15, total: 10 }, 100],
  ])("normalizes progress %#", (event, expected) => {
    const onProgress = vi.fn();
    uploadVideo(new File(["x"], "x.mp4"), { candidate_fps: 5, selection_window_seconds: 1 }, "web-request", onProgress);
    ProgressXHR.latest.progress?.(event as ProgressEvent);
    expect(onProgress).toHaveBeenCalledWith(expected);
  });
});
