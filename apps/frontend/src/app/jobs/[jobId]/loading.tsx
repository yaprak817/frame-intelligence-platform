export default function JobLoading() {
  return (
    <div className="page-shell job-shell" role="status" aria-live="polite">
      <div className="skeleton skeleton-title" />
      <div className="skeleton skeleton-card" />
      <span className="sr-only">İş durumu yükleniyor</span>
    </div>
  );
}
