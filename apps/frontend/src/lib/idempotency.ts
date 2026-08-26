export interface SubmissionIdentity {
  signature: string;
  key: string;
}

export function idempotencyKeyFor(
  signature: string,
  current: SubmissionIdentity | null,
): SubmissionIdentity {
  if (current?.signature === signature) return current;
  return { signature, key: `web-${crypto.randomUUID()}` };
}

export function urlSignature(url: string, candidateFps: number, windowSeconds: number) {
  return JSON.stringify(["URL", url, candidateFps, windowSeconds]);
}

export function uploadSignature(file: File, candidateFps: number, windowSeconds: number) {
  return JSON.stringify([
    "UPLOAD",
    file.name,
    file.type,
    file.size,
    file.lastModified,
    candidateFps,
    windowSeconds,
  ]);
}
