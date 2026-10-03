import { BrandAnnotationGallery } from "@/components/annotations/brand-annotation-gallery";

export default async function BrandAnnotationsPage({
  params,
}: {
  params: Promise<{ brandId: string }>;
}) {
  const { brandId } = await params;

  return <BrandAnnotationGallery brandId={brandId} />;
}
