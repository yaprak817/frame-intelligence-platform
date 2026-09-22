import { JobSubmission } from "@/components/submission/job-submission";

export default function NewJobPage() {
  return (
    <div className="page-shell landing-grid">
      <section className="hero" aria-labelledby="page-title">
        <p className="eyebrow">Akıllı kare seçimi</p>
        <h1 id="page-title">Videodan değerli anları ayırın.</h1>
        <p className="hero-copy">
          Bir video, video bağlantısı veya görsel veri seti gönderin.
        </p>
      </section>
      <JobSubmission />
    </div>
  );
}
