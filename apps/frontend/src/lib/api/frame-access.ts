import { createFrameAccess } from "./client";
import type { FrameAccessResponse, ResultFrame } from "./types";

export const FRAME_ACCESS_CONCURRENCY = 3;

export class FrameAccessLoader {
  private readonly inFlight = new Map<number, Promise<FrameAccessResponse>>();
  private readonly cache = new Map<number, FrameAccessResponse>();
  private readonly queue: Array<{ run: () => void; reject: (reason: unknown) => void }> = [];
  private active = 0;

  constructor(private readonly jobId: string, private readonly signal: AbortSignal) {
    signal.addEventListener("abort", () => {
      const reason = signal.reason ?? new DOMException("Aborted", "AbortError");
      for (const task of this.queue.splice(0)) task.reject(reason);
    }, { once: true });
  }

  private drain() {
    while (this.active < FRAME_ACCESS_CONCURRENCY && this.queue.length) {
      const task = this.queue.shift()!;
      if (this.signal.aborted) task.reject(this.signal.reason ?? new DOMException("Aborted", "AbortError"));
      else task.run();
    }
  }

  load(frame: ResultFrame, refresh = false): Promise<FrameAccessResponse> {
    if (this.signal.aborted) return Promise.reject(this.signal.reason ?? new DOMException("Aborted", "AbortError"));
    if (!refresh) {
      const cached = this.cache.get(frame.index);
      if (cached && Date.parse(cached.expires_at) > Date.now()) return Promise.resolve(cached);
    } else {
      this.cache.delete(frame.index);
    }
    const existing = this.inFlight.get(frame.index);
    if (existing) return existing;
    const pending = new Promise<FrameAccessResponse>((resolve, reject) => {
      const run = () => {
        this.active += 1;
        createFrameAccess(this.jobId, frame.index, this.signal).then((access) => {
          if (access.content_type !== frame.content_type || access.size_bytes !== frame.size_bytes || access.sha256 !== frame.sha256) throw new Error("FRAME_METADATA_MISMATCH");
          this.cache.set(frame.index, access);
          return access;
        }).then(resolve, reject).finally(() => {
          this.active -= 1;
          this.drain();
        });
      };
      if (this.active < FRAME_ACCESS_CONCURRENCY) run();
      else this.queue.push({ run, reject });
    }).finally(() => this.inFlight.delete(frame.index));
    this.inFlight.set(frame.index, pending);
    return pending;
  }

  async loadMany(frames: ResultFrame[]): Promise<FrameAccessResponse[]> {
    const results = new Array<FrameAccessResponse>(frames.length);
    let cursor = 0;
    const worker = async () => {
      while (cursor < frames.length) {
        const index = cursor++;
        results[index] = await this.load(frames[index]);
      }
    };
    await Promise.all(Array.from({ length: Math.min(FRAME_ACCESS_CONCURRENCY, frames.length) }, worker));
    return results;
  }
}
