import { ResultView } from "@/components/results/result-view";

export default async function ResultPage({ params, searchParams }: { params: Promise<{ jobId: string }>; searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const { jobId } = await params;
  const query = await searchParams;
  const code = query.download_error;
  const rawRetry = query.retry_after;
  const retry = typeof rawRetry === "string" && /^\d+$/.test(rawRetry) ? Number(rawRetry) : 60;
  const bounded = Number.isSafeInteger(retry) && retry >= 1 && retry <= 86_400 ? retry : 60;
  const error = code === "RATE_LIMITED"
    ? `Çok fazla istek gönderildi. ${bounded} saniye bekleyip tekrar deneyin.`
    : code === "DOWNLOAD_FAILED" ? "ZIP indirilemedi. Lütfen tekrar deneyin." : null;
  return <ResultView jobId={jobId} downloadError={error} />;
}
