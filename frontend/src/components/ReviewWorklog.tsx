import { useEffect, useRef, useState } from 'react';
import {
  BookOpen,
  CircleCheck,
  CircleHelp,
  ClipboardCheck,
  FileText,
  Globe,
  Library,
  ListChecks,
  RotateCcw,
  ScrollText,
  Search,
  TriangleAlert,
} from 'lucide-react';
import type { ReviewTaskApi } from '../types/api';
import { getReviewTask } from '../api/client';

const ACTION_META: Record<string, { label: string; live: string; Icon: typeof Search }> = {
  propose_plan: { label: '制定计划', live: '正在制定调查计划', Icon: ListChecks },
  read_material: { label: '复核材料', live: '正在复核冻结材料', Icon: FileText },
  search_evidence: { label: '检索法源', live: '正在检索法源', Icon: Search },
  read_evidence: { label: '研读条文', live: '正在研读条文', Icon: BookOpen },
  search_web: { label: '联网核查', live: '正在联网核查最新口径', Icon: Globe },
  queue_enrichment: { label: '补充知识库', live: '正在提交官方来源供后台补库', Icon: Library },
  finish: { label: '核验成文', live: '正在核验成稿的法律引用与事实一致性，并生成审查报告', Icon: ClipboardCheck },
  tool_error: { label: '参数修正', live: '正在修正工具参数后重试', Icon: RotateCcw },
  request_input: { label: '等待补充', live: '正在等待申请人补充关键事实', Icon: CircleHelp },
};

const ACTION_META_FALLBACK = (action: string) => ({ label: action, live: `正在执行 ${action}`, Icon: Search });

function meta(action: string) {
  return ACTION_META[action] ?? ACTION_META_FALLBACK(action);
}

/** 底部统计行的动作 → 图标与文案。 */
const FOOTER_STATS: Array<{ action: string; label: string; unit: string; Icon: typeof Search }> = [
  { action: 'search_evidence', label: '检索', unit: '次', Icon: Search },
  { action: 'read_evidence', label: '研读条文', unit: '次', Icon: BookOpen },
  { action: 'read_material', label: '复核材料', unit: '次', Icon: FileText },
  { action: 'search_web', label: '联网核查', unit: '次', Icon: Globe },
  { action: 'queue_enrichment', label: '补充知识库', unit: '项', Icon: Library },
  { action: 'tool_error', label: '参数修正', unit: '次', Icon: RotateCcw },
];

function formatElapsed(startedAt: string, now: number): string {
  const started = new Date(startedAt).getTime();
  if (!Number.isFinite(started)) return '';
  const total = Math.max(0, Math.floor((now - started) / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`;
}

/** 审查工作底稿：Agent 调查过程的实时时间线，审查完成后淡出并把页面交还给正式报告。 */
export default function ReviewWorklog({ taskId, onSettled }: { taskId: string; onSettled: () => void }): JSX.Element {
  const [task, setTask] = useState<ReviewTaskApi | null>(null);
  const [settled, setSettled] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const settledRef = useRef(false);
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = async (): Promise<void> => {
      try {
        const next = await getReviewTask(taskId);
        if (!alive) return;
        setTask(next);
        const finished = next.status === 'succeeded' || next.status === 'failed';
        if (finished && !settledRef.current) {
          settledRef.current = true;
          setSettled(true);
          timer = window.setTimeout(() => { if (alive) onSettledRef.current(); }, 1500);
          return;
        }
        if (finished) return;
      } catch { /* 网络抖动：下一轮再取 */ }
      if (alive) timer = window.setTimeout(tick, 4000);
    };
    void tick();
    return () => { alive = false; if (timer !== undefined) window.clearTimeout(timer); };
  }, [taskId]);

  const active = task?.status === 'running';
  useEffect(() => {
    if (!active) return undefined;
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(clock);
  }, [active]);

  const failed = task?.status === 'failed';
  const queued = task?.status === 'queued';
  const currentNode = task?.current_node ?? null;
  const currentMeta = currentNode ? meta(currentNode) : null;
  const footer = FOOTER_STATS
    .map(({ action, label, unit, Icon }) => {
      const count = task?.steps.filter((step) => step.action === action).length ?? 0;
      return count > 0 ? { key: label, text: `${label} ${count} ${unit}`, Icon } : null;
    })
    .filter((entry): entry is { key: string; text: string; Icon: typeof Search } => entry !== null);
  const CurrentIcon = queued ? ScrollText : currentMeta?.Icon ?? ScrollText;

  return (
    <section className={'card review-worklog' + (settled ? ' review-worklog--settled' : '')} role="status" aria-label="审查工作底稿">
      <div className="review-worklog__head">
        <h2><ScrollText size={17} strokeWidth={1.8} aria-hidden="true" />审查工作底稿</h2>
        <span className="review-worklog__meta">
          {settled
            ? <span className={'review-worklog__state' + (failed ? ' review-worklog__state--failed' : '')}>{failed ? '审查未完成' : '审查完成'}</span>
            : <><span className="review-worklog__pulse" aria-hidden="true" />{queued ? '排队中' : '审查中'}{task && !queued ? ` · 已用 ${formatElapsed(task.created_at, now)}` : ''}</>}
        </span>
      </div>
      <ol className="review-worklog__timeline">
        {task?.steps.map((step) => {
          const stepMeta = meta(step.action);
          return (
            <li key={step.number} className={step.action === 'tool_error' ? 'is-retry' : undefined}>
              <span className="review-worklog__icon" aria-hidden="true"><stepMeta.Icon size={13} strokeWidth={1.8} /></span>
              <div>
                <small>{stepMeta.label}</small>
                <p>{step.summary}</p>
              </div>
            </li>
          );
        })}
        {!settled && !failed ? (
          <li className="is-current" aria-live="polite">
            <span className="review-worklog__icon" aria-hidden="true"><CurrentIcon size={13} strokeWidth={1.8} /></span>
            <div>
              <small>{queued ? '排队中' : currentMeta?.label ?? '准备中'}</small>
              <p>{queued ? '任务已提交，等待审查进程领取' : currentMeta?.live ?? '正在准备审查上下文'}</p>
            </div>
          </li>
        ) : null}
        {settled && !failed ? (
          <li className="is-done">
            <span className="review-worklog__icon" aria-hidden="true"><CircleCheck size={14} strokeWidth={1.8} /></span>
            <div>
              <small>核验成文</small>
              <p>报告已成稿，正在转入审查报告视图</p>
            </div>
          </li>
        ) : null}
        {failed && task ? (
          <li className="is-failed">
            <span className="review-worklog__icon" aria-hidden="true"><TriangleAlert size={13} strokeWidth={1.8} /></span>
            <div>
              <small>失败原因</small>
              <p>{task.error_message || '审查任务失败，可由审核人重试。'}</p>
            </div>
          </li>
        ) : null}
      </ol>
      {footer.length ? (
        <div className="review-worklog__foot">
          {footer.map(({ key, text, Icon }) => <span key={key}><Icon size={12} strokeWidth={1.8} aria-hidden="true" />{text}</span>)}
        </div>
      ) : null}
    </section>
  );
}
