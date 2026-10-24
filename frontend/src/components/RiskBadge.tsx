/**
 * RiskBadge — small reusable risk-level indicator.
 *
 * Renders the standard `.badge` pill (defined in `styles/primitives.css`) with
 * the tone-on-tint color matching the backend `RiskLevel` enum. Labels and
 * classes come from `utils/display.ts` — the single source for risk wording.
 */
import type { RiskLevel } from '../types/api';
import { RISK_LABELS, RISK_BADGE_CLASS } from '../utils/display';

export interface RiskBadgeProps {
  level: RiskLevel;
}

export default function RiskBadge({ level }: RiskBadgeProps): JSX.Element {
  return <span className={RISK_BADGE_CLASS[level] ?? RISK_BADGE_CLASS.insufficient_evidence}>{RISK_LABELS[level] ?? RISK_LABELS.insufficient_evidence}</span>;
}
