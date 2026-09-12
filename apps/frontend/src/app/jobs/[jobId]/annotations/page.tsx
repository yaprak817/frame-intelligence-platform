import { AnnotationGallery } from "@/components/annotations/annotation-gallery";

export default async function AnnotationPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await params;
  return <AnnotationGallery jobId={jobId} />;
}
