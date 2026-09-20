import { BrandDetailClient } from "./brand-detail-client";

export default async function BrandDetailPage({params}:{params:Promise<{brandId:string}>}){
  const {brandId}=await params;
  return <BrandDetailClient brandId={brandId}/>;
}
