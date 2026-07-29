// Shared model selector for the Science & Lab start forms: one dropdown for the
// orchestrator/director model, plus a chip grid for the specialist pool (click to
// toggle). Empty pool = specialists use the orchestrator model.

export function ModelPicker(props: {
  models: string[];
  orchLabel: string;                 // e.g. "Orchestrator model" / "Director model"
  orchModel: string;
  setOrchModel: (m: string) => void;
  pool: string[];
  setPool: (updater: (p: string[]) => string[]) => void;
}) {
  if (!props.models.length) return null;
  const toggle = (m: string) =>
    props.setPool((p) => (p.includes(m) ? p.filter((x) => x !== m) : [...p, m]));

  return (
    <div className="model-picker">
      <label>{props.orchLabel}</label>
      <select value={props.orchModel} onChange={(e) => props.setOrchModel(e.target.value)}>
        {props.models.map((m) => (
          <option key={m} value={m}>{m}</option>
        ))}
      </select>

      <label style={{ marginTop: 12 }}>
        Specialist pool{" "}
        <span className="field-hint">
          — models the {props.orchLabel.split(" ")[0].toLowerCase()} can assign to
          specialists. None selected = they use the {props.orchLabel.split(" ")[0].toLowerCase()} model.
        </span>
      </label>
      <div className="model-chips">
        {props.models.map((m) => {
          const on = props.pool.includes(m);
          return (
            <button
              type="button"
              key={m}
              className={"model-chip" + (on ? " on" : "")}
              aria-pressed={on}
              onClick={() => toggle(m)}
            >
              {m}
            </button>
          );
        })}
      </div>
      {props.pool.length > 0 && (
        <div className="pool-actions">
          <span className="muted">{props.pool.length} in pool</span>
          <button type="button" className="linkbtn" onClick={() => props.setPool(() => [])}>
            clear
          </button>
        </div>
      )}
    </div>
  );
}
