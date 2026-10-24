import { useNavigate } from 'react-router-dom';
import { ChevronRight, ClipboardCheck, FilePlus2, MessageSquare, X } from 'lucide-react';

const DISMISSED_KEY = 'crosscomply.onboarding.dismissed';

export function onboardingDismissed(): boolean {
  return window.localStorage.getItem(DISMISSED_KEY) === '1';
}

export function dismissOnboarding(): void {
  window.localStorage.setItem(DISMISSED_KEY, '1');
}

const STEPS = [
  {
    icon: MessageSquare,
    title: '先提问，弄清风险',
    description: '在合规问答里描述业务场景，Agent 会引用现行法条作答，并标注依据来源。',
    target: '/qa',
    action: '开始提问',
  },
  {
    icon: FilePlus2,
    title: '发起正式审查',
    description: '确定需要正式结论时，在新建案件里上传材料，Agent 依据法律出具审查报告。',
    target: '/workbench',
    action: '去新建案件',
  },
  {
    icon: ClipboardCheck,
    title: '跟进待办事项',
    description: '审查要求补充事实或整改时，待办会出现在「我的复核」，逐项处理后归档。',
    target: '/reviews',
    action: '查看我的复核',
  },
];

/** 首次使用的三步引导：可在问答页随时从「使用指引」重新打开。 */
export default function QaOnboarding({ onDismiss }: { onDismiss: () => void }): JSX.Element {
  const navigate = useNavigate();
  return (
    <section className="qa-onboarding" aria-label="使用指引">
      <div className="qa-onboarding__head">
        <strong>三步开始使用</strong>
        <button type="button" className="qa-quiet-button" aria-label="关闭使用指引" onClick={onDismiss}><X size={15} /></button>
      </div>
      <ol className="qa-onboarding__steps">
        {STEPS.map((step, index) => (
          <li key={step.title}>
            <span className="qa-onboarding__icon" aria-hidden="true"><step.icon size={18} strokeWidth={1.8} /></span>
            <div>
              <strong>{index + 1}. {step.title}</strong>
              <p>{step.description}</p>
            </div>
            <button type="button" onClick={() => { onDismiss(); navigate(step.target); }}>
              {step.action}<ChevronRight size={15} aria-hidden="true" />
            </button>
          </li>
        ))}
      </ol>
      <p className="qa-onboarding__foot">系统仅提供合规决策辅助，不替代专业法律意见。完成后此卡片不再自动出现，可随时从下方「使用指引」重新打开。</p>
    </section>
  );
}
