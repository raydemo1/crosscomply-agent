const USAGE_SENTENCES = [
  /^本结论基于当前材料(?:和|及|、)\s*已召回证据[，,、]?\s*不构成正式法律意见[。]?$/u,
  /^(?:本报告|本结论)(?:仅供参考[，,])?不构成正式法律意见[。]?$/u,
  /^本报告由\s*CrossComply\s*自动生成[，,]仅供研究参考[，,]不构成正式法律意见[。]?$/u,
  /^系统仅提供合规决策辅助[，,]不替代专业法律意见[。]?$/u,
];

export function partitionReportContent(conclusion: string, boundaries: readonly string[]) {
  const usageNotes: string[] = [];
  const separate = (value: string): string => (value.match(/[^\n。！？]*(?:[。！？](?:\*\*|__)?|\n|$)/gu) ?? []).filter((part) => {
    const sentence = part.trim().replace(/^[-*+]\s+/u, '').replace(/\*\*|__/gu, '');
    if (!USAGE_SENTENCES.some((pattern) => pattern.test(sentence))) return true;
    if (!usageNotes.includes(sentence)) usageNotes.push(sentence);
    return false;
  }).join('').replace(/\n{3,}/gu, '\n\n').trim();
  const body = separate(conclusion);
  const conditions = Array.from(new Set(boundaries.map(separate).filter(Boolean)));
  return { body, conditions, usageNotes };
}
