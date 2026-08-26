import type { ApiErrorPayload } from "./types";

const CODE_MESSAGES: Record<string, string> = {
  JOB_NOT_FOUND: "İş bulunamadı.",
  UNSAFE_URL: "Bu video bağlantısı güvenli bulunmadı.",
  UNSUPPORTED_SOURCE: "Bu video kaynağı desteklenmiyor.",
  INVALID_VIDEO: "Video doğrulanamadı veya bozuk.",
  SOURCE_TOO_LARGE: "Video izin verilen boyuttan büyük.",
  DOWNLOAD_TIMEOUT: "Video indirme işlemi zaman aşımına uğradı.",
  DOWNLOAD_FAILED: "Video indirilemedi.",
  PROCESSING_FAILED: "Video işlenirken bir sorun oluştu.",
  STORAGE_UNAVAILABLE: "Dosya servisine şu anda ulaşılamıyor.",
};

const STATUS_MESSAGES: Record<number, string> = {
  404: "İş bulunamadı.",
  409: "Bu gönderim daha önce farklı içerikle kullanılmış.",
  413: "Video izin verilen boyuttan büyük.",
  415: "Bu video biçimi desteklenmiyor.",
  422: "Lütfen form alanlarını kontrol edin.",
  503: "Hizmete şu anda ulaşılamıyor. Lütfen yeniden deneyin.",
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(code ? CODE_MESSAGES[code] ?? STATUS_MESSAGES[status] : STATUS_MESSAGES[status]);
    this.name = "ApiError";
  }
}

export function safeApiError(status: number, payload?: ApiErrorPayload): ApiError {
  const detail = payload?.detail;
  const code =
    detail && !Array.isArray(detail) && typeof detail === "object"
      ? typeof detail.code === "string"
        ? detail.code
        : undefined
      : undefined;
  return new ApiError(status, code);
}

export function userErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message || STATUS_MESSAGES[error.status] || "İstek tamamlanamadı.";
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return "İstek iptal edildi.";
  }
  return "Sunucuya ulaşılamadı. Bağlantınızı kontrol edip yeniden deneyin.";
}

export function failureMessage(code: string): string {
  return CODE_MESSAGES[code] ?? "İşleme tamamlanamadı.";
}
