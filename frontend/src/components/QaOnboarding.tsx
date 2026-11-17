import { useNavigate } from 'react-router-dom';
import { ChevronRight, ClipboardCheck, FilePlus2, MessageSquare } from 'lucide-react';

const STEPS = [
  {
    icon: MessageSquare,
    target: '/qa',
    action: '开始提问',
  },
  {
    icon: FilePlus2,
    target: '/workbench',
    action: '新建案件',
  },
  {
    icon: ClipboardCheck,
    target: '/reviews',
    action: '查看待办',
  },
];

export default function QaOnboarding({ userRole, onDismiss, onStartQuestion }: {
  userRole: 'requester' | 'reviewer' | 'admin';
  onDismiss: () => void;
  onStartQuestion: () => void;
}): JSX.Element {
  const navigate = useNavigate();
  return (
    <section className="qa-onboarding" aria-label="使用指引">
      <ul className="qa-onboarding__steps" role="list">
        {STEPS.map((step) => (
          <li key={step.target}>
            <button type="button" onClick={() => {
              onDismiss();
              if (step.target === '/qa') onStartQuestion();
              else navigate(step.target);
            }}>
              <step.icon size={17} strokeWidth={1.8} aria-hidden="true" />
              <span>{step.target === '/reviews' ? userRole === 'requester' ? '查看我的待办' : '查看待办与复核' : step.action}</span>
              <ChevronRight size={15} aria-hidden="true" />
            </button>
          </li>
        ))}
      </ul>
      <p className="qa-onboarding__foot">本系统提供合规审查辅助，不替代正式法律意见。</p>
    </section>
  );
}
