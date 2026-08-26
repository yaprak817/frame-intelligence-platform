"use client";

export default function JobError({ reset }: { reset: () => void }) {
  return (
    <div className="page-shell error-page" role="alert">
      <h1>İş durumu gösterilemedi</h1>
      <p>Bağlantıyı kontrol edip yeniden deneyin.</p>
      <button className="button primary" onClick={reset}>Yeniden dene</button>
    </div>
  );
}
