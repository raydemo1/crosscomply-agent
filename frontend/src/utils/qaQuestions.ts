import type { QAQuestion } from '../types/qa.ts';

export function toggleQAOption(current: string[], option: string, selection: QAQuestion['selection']): string[] {
  if (current.includes(option)) return selection === 'single' ? current : current.filter((value) => value !== option);
  if (selection === 'single' || option === '不清楚') return [option];
  return [...current.filter((value) => value !== '不清楚'), option];
}

export function formatQAAnswers(questions: QAQuestion[], answers: Record<number, string[]>, custom: Record<number, string>): string | null {
  if (questions.some((_, i) => !answers[i]?.length && !custom[i]?.trim())) return null;
  return questions.map((item, i) => [
    `针对助手提问：${item.question}`,
    answers[i]?.length ? `用户选择：${answers[i].join('；')}` : '',
    custom[i]?.trim() ? `用户补充：${custom[i].trim()}` : '',
  ].filter(Boolean).join('\n')).join('\n\n');
}

export function qaQuestionOptions(options: string[]): string[] {
  return [...new Set([...options, '不清楚'])];
}
