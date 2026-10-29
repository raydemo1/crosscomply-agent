import { useNavigate } from 'react-router-dom';
import { ChevronRight, ClipboardCheck, FilePlus2, MessageSquare, X } from 'lucide-react';

const STEPS = [
  {
    icon: MessageSquare,
    title: '咨询合规问题',
    target: '/qa',
    action: '开始提问',
  },
  {
    icon: FilePlus2,
    title: '提交合规审查',
    target: '/workbench',
    action: '新建案件',
  },
  {
    icon: ClipboardCheck,
    title: '跟进待办事项',
    target: '/reviews',
    action: '查看待办',
  },
];

export default function QaOnboarding({ userRole, onDismiss }: { userRole: 'requester' | 'reviewer' | 'admin'; onDismiss: () => void }): JSX.Element {
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

            </div>
            <button type="button" onClick={() => { onDismiss(); navigate(step.target); }}>
              {step.target === '/reviews' ? userRole === 'requester' ? '查看我的待办' : '查看待办与复核' : step.action}<ChevronRight size={15} aria-hidden="true" />
            </button>
          </li>
        ))}
      </ol>
      <p className="qa-onboarding__foot">本系统提供合规审查辅助，不替代正式法律意见。</p>
    </section>
  );
}
