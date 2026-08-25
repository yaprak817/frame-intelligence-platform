interface ProcessingFieldsProps {
  candidateFps: string;
  windowSeconds: string;
  disabled: boolean;
  errors: Record<string, string>;
  onCandidateFps: (value: string) => void;
  onWindowSeconds: (value: string) => void;
}

export function ProcessingFields(props: ProcessingFieldsProps) {
  return (
    <fieldset className="processing-fields" disabled={props.disabled}>
      <legend>İşleme ayarları</legend>
      <div className="field-grid">
        <div className="field">
          <label htmlFor="candidate-fps">Aday kare / saniye</label>
          <input
            id="candidate-fps"
            name="candidate_fps"
            type="number"
            min="0.01"
            max="60"
            step="0.01"
            value={props.candidateFps}
            aria-invalid={Boolean(props.errors.candidateFps)}
            aria-describedby={props.errors.candidateFps ? "candidate-fps-error" : "candidate-fps-help"}
            onChange={(event) => props.onCandidateFps(event.target.value)}
          />
          <span id="candidate-fps-help" className="hint">0&apos;dan büyük, en fazla 60; varsayılan 5</span>
          {props.errors.candidateFps && <span id="candidate-fps-error" className="field-error">{props.errors.candidateFps}</span>}
        </div>
        <div className="field">
          <label htmlFor="window-seconds">Seçim penceresi (saniye)</label>
          <input
            id="window-seconds"
            name="selection_window_seconds"
            type="number"
            min="0.01"
            max="3600"
            step="0.01"
            value={props.windowSeconds}
            aria-invalid={Boolean(props.errors.windowSeconds)}
            aria-describedby={props.errors.windowSeconds ? "window-error" : "window-help"}
            onChange={(event) => props.onWindowSeconds(event.target.value)}
          />
          <span id="window-help" className="hint">0&apos;dan büyük, en fazla 3600; varsayılan 1</span>
          {props.errors.windowSeconds && <span id="window-error" className="field-error">{props.errors.windowSeconds}</span>}
        </div>
      </div>
    </fieldset>
  );
}
