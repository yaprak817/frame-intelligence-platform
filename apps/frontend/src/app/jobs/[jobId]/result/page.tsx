import { ResultGallery } from "@/components/results/result-gallery";

export default async function ResultPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await params;
  return <ResultGallery jobId={jobId} />;
}
