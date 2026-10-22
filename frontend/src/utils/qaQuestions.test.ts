import assert from 'node:assert/strict';
import test from 'node:test';
import { formatQAAnswers, qaQuestionOptions, toggleQAOption } from './qaQuestions.ts';

test('single choice replaces earlier selection; multiple choice toggles and excludes unknown', () => {
  assert.deepEqual(toggleQAOption(['甲'], '乙', 'single'), ['乙']);
  assert.deepEqual(toggleQAOption(['甲'], '甲', 'single'), ['甲']);
  assert.deepEqual(toggleQAOption(['甲'], '乙', 'multiple'), ['甲', '乙']);
  assert.deepEqual(toggleQAOption(['甲', '乙'], '甲', 'multiple'), ['乙']);
  assert.deepEqual(toggleQAOption(['甲', '乙'], '不清楚', 'multiple'), ['不清楚']);
  assert.deepEqual(toggleQAOption(['不清楚'], '甲', 'multiple'), ['甲']);
});

test('submission requires each fact and preserves original questions, selection and custom wording', () => {
  const questions = [{ question: '实际累计人数？', selection: 'single' as const, options: ['已统计', '未统计'] },
    { question: '涉及的数据？', selection: 'multiple' as const, options: ['员工', '客户'] }];
  assert.equal(formatQAAnswers(questions, { 0: ['不清楚'] }, {}), null);
  assert.equal(formatQAAnswers(questions, { 0: ['不清楚'], 1: ['员工', '客户'] }, {}),
    '针对助手提问：实际累计人数？\n用户选择：不清楚\n\n针对助手提问：涉及的数据？\n用户选择：员工；客户');
  assert.equal(formatQAAnswers([questions[0]], {}, { 0: '  实际1200人  ' }),
    '针对助手提问：实际累计人数？\n用户补充：实际1200人');
});

test('model-supplied unknown option appears only once', () => {
  assert.deepEqual(qaQuestionOptions(['是', '否', '不清楚']), ['是', '否', '不清楚']);
});

test('submission labels assistant assumptions separately from actual user reply', () => {
  const questions = [{ question: '既然已有集体合同，覆盖哪些人员？', selection: 'single' as const,
    options: ['全部员工', '部分员工'] }];
  assert.equal(formatQAAnswers(questions, { 0: ['不清楚'] }, { 0: '更正：是否存在集体合同也不清楚' }),
    '针对助手提问：既然已有集体合同，覆盖哪些人员？\n用户选择：不清楚\n用户补充：更正：是否存在集体合同也不清楚');
});
