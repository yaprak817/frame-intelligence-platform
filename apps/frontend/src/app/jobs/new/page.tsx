import { JobSubmission } from "@/components/submission/job-submission";

export default function NewJobPage() {
  return (
    <div className="page-shell landing-grid">
      <section className="hero" aria-labelledby="page-title">
        <p className="eyebrow">Ak?ll? kare se?imi</p>
        <h1 id="page-title">Videodan de?erli anlar? ay?r?n.</h1>
        <p className="hero-copy">
          Bir video, video ba?lant?s? veya g?rsel veri seti g?nderin.
        </p>
      </section>
      <JobSubmission />
    </div>
  );
}
