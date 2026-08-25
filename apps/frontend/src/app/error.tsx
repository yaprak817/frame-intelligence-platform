"use client";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <div className="page-shell error-page" role="alert">
      <h1>Sayfa yüklenemedi</h1>
      <p>Beklenmeyen bir sorun oluştu. Güvenle yeniden deneyebilirsiniz.</p>
      <button className="button primary" onClick={reset}>Yeniden dene</button>
    </div>
  );
}
