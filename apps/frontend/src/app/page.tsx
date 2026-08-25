import { JobSubmission } from "@/components/submission/job-submission";

export default function HomePage() {
  return (
    <div className="page-shell landing-grid">
      <section className="hero" aria-labelledby="page-title">
        <p className="eyebrow">Akıllı kare seçimi</p>
        <h1 id="page-title">Videodan değerli anları ayırın.</h1>
        <p className="hero-copy">
          Bir video yükleyin veya bağlantı gönderin. İşleme durumunu iş kimliğiyle
          güvenli ve kesintisiz biçimde takip edin.
        </p>
        <ul className="feature-list">
          <li>Kalite ve çeşitlilik odaklı seçim</li>
          <li>Asenkron, yeniden açılabilir iş takibi</li>
          <li>Teknik ayrıntıları gizleyen anlaşılır durumlar</li>
        </ul>
      </section>
      <JobSubmission />
    </div>
  );
}
