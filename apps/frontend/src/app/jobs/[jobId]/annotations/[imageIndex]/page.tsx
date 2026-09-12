import { notFound } from "next/navigation";
import { AnnotationWorkspace } from "@/components/annotations/annotation-workspace";

export default async function AnnotationEditorPage({ params }: { params: Promise<{ jobId: string; imageIndex: string }> }) {
  const { jobId, imageIndex } = await params;
  if (!/^(?:0|[1-9]\d*)$/.test(imageIndex) || !Number.isSafeInteger(Number(imageIndex))) notFound();
  return <AnnotationWorkspace jobId={jobId} initialImageIndex={Number(imageIndex)} />;
}
