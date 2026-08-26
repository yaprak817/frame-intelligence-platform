import { JobTracker } from "@/components/jobs/job-tracker";

export default async function JobPage({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const { jobId } = await params;
  return <JobTracker jobId={jobId} />;
}
