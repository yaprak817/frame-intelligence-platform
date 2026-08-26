"use client";
export default function ResultError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <main className="page-shell error-page"><p className="eyebrow">Bir sorun oluştu</p><h1>Sonuç sayfası açılamadı</h1><div className="alert error" role="alert">Güvenli biçimde yeniden deneyebilirsiniz.</div><button className="button primary" onClick={reset}>Yeniden dene</button></main>;
}
