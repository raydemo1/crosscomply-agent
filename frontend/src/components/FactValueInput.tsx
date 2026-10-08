import { FACT_FIELDS, type FactField } from '../utils/factFields';

export default function FactValueInput({ field, value, onChange, disabled = false }: {
  field: FactField; value: unknown; onChange: (value: unknown) => void; disabled?: boolean;
}) {
  const spec = FACT_FIELDS[field];
  const options = spec.kind === 'boolean' ? [['', '尚不确定'], ['true', '是'], ['false', '否']] : spec.options;
  if (options) return <select aria-label={spec.label} disabled={disabled} value={value == null && spec.kind === 'boolean' ? '' : String(value ?? 'unknown')} onChange={(e) => onChange(spec.kind === 'boolean' ? (e.target.value === '' ? null : e.target.value === 'true') : e.target.value)}>
    {options.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
  </select>;
  return <textarea aria-label={spec.label} disabled={disabled} rows={2} value={Array.isArray(value) ? value.join('、') : String(value ?? '')} onChange={(e) => onChange(spec.kind === 'list' ? e.target.value.split(/[、,，\n]/).map((v) => v.trim()).filter(Boolean) : e.target.value)} />;
}
