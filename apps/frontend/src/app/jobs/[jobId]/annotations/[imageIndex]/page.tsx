import { notFound } from "next/navigation";
import { AnnotationWorkspace } from "@/components/annotations/annotation-workspace";

const SAFE_GALLERY_RETURN =
  /^\/(?:jobs|brands)\/[^/?#]+\/annotations(?:\?[^#]*)?$/;

export default async function AnnotationEditorPage({
  params,
  searchParams,
}: {
  params: Promise<{ jobId: string; imageIndex: string }>;
  searchParams: Promise<{ returnTo?: string | string[] }>;
}) {
  const { jobId, imageIndex } = await params;
  const query = await searchParams;

  if (
    !/^(?:0|[1-9]\d*)$/.test(imageIndex) ||
    !Number.isSafeInteger(Number(imageIndex))
  ) {
    notFound();
  }

  const rawReturnTo =
    typeof query.returnTo === "string" ? query.returnTo : undefined;
  const returnTo =
    rawReturnTo && SAFE_GALLERY_RETURN.test(rawReturnTo)
      ? rawReturnTo
      : undefined;

  return (
    <AnnotationWorkspace
      jobId={jobId}
      initialImageIndex={Number(imageIndex)}
      returnTo={returnTo}
    />
  );
}
