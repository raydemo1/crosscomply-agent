import { useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import type { CaseIntake, DashboardSummaryApi } from '../types/api';
import { validateUploadFile } from '../api/client';

interface WorkbenchPageProps {
  question: string;
  material: string;
  intake: CaseIntake;
  reviewMode: 'llm' | 'multi_agent';
  rerankMode: 'off' | 'embedding';
  editingCaseId: string | null;
  onQuestionChange: (value: string) => void;
  onMaterialChange: (value: string) => void;
  onIntakeChange: (value: CaseIntake) => void;
  onReviewModeChange: (mode: 'llm' | 'multi_agent') => void;
  onRerankModeChange: (mode: 'off' | 'embedding') => void;
  onSubmit: (question: string, material: string, intake: CaseIntake, file?: File | null) => void;
  loading: boolean;
  error: string | null;
  historyCount: number;
  summary: DashboardSummaryApi | null;
}

function updateIntake(intake: CaseIntake, onChange: (next: CaseIntake) => void, key: keyof CaseIntake, value: string | boolean | null | string[]): void {
  onChange({ ...intake, [key]: value });
}

export default function WorkbenchPage({
  question,
  material,
  intake,
  reviewMode,
  rerankMode,
  editingCaseId,
  onQuestionChange,
  onMaterialChange,
  onIntakeChange,
  onReviewModeChange,
  onRerankModeChange,
  onSubmit,
  loading,
  error,
  historyCount,
  summary,
}: WorkbenchPageProps): JSX.Element {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [step, setStep] = useState<1 | 2>(1);
  const canSubmit = !loading && Boolean(question.trim()) && Boolean(material.trim() || selectedFile);

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      validateUploadFile(file);
      setSelectedFile(file);
      setFileError(null);
    } catch (errorValue) {
      setSelectedFile(null);
      setFileError(errorValue instanceof Error ? errorValue.message : '文件校验失败');
    }
  };

  const submit = (): void => {
    if (canSubmit) onSubmit(question.trim(), material.trim(), intake, selectedFile);
  };

  return (
    <div className="workbench" aria-busy={loading}>
      <header className="workspace-hero">
        <div>
          <div className="report-kicker">Cross-border review desk</div>
          <h1>{editingCaseId ? '补充当前案件，让审查继续向前。' : '把一次合规问题，变成一份可追踪的案件记录。'}</h1>
          <p>从业务材料开始，确认关键事实，生成有证据依据的审查结论与后续动作。</p>
        </div>
        <div className="workspace-hero__metric"><strong>{summary?.total_cases ?? historyCount}</strong><span>已纳入案件</span></div>
        <div className="workspace-hero__risk-strip">
          <span><i className="risk-dot risk-dot--high" />高风险 <strong>{summary?.risk_counts.high ?? 0}</strong></span>
          <span><i className="risk-dot risk-dot--medium" />中风险 <strong>{summary?.risk_counts.medium ?? 0}</strong></span>
          <span><i className="risk-dot risk-dot--insufficient" />待补充 <strong>{summary?.risk_counts.insufficient_evidence ?? 0}</strong></span>
        </div>
      </header>

      <section className="intake-progress card">
        <div className={'intake-progress__step' + (step === 1 ? ' is-active' : ' is-done')}><span>01</span><div><strong>建立案件</strong><small>问题与材料</small></div></div>
        <div className="intake-progress__line" />
        <div className={'intake-progress__step' + (step === 2 ? ' is-active' : '')}><span>02</span><div><strong>确认要素</strong><small>范围与责任边界</small></div></div>
        <div className="intake-progress__line" />
        <div className="intake-progress__step"><span>03</span><div><strong>提交审查</strong><small>证据与行动</small></div></div>
      </section>

      {error ? <div className="error-box" role="alert"><span className="error-box__mark">!</span><div>{error}</div></div> : null}

      {step === 1 ? (
        <section className="card intake-card">
          <div className="section-heading-row"><div><div className="report-kicker">01 / 案件入口</div><h2>先把业务说清楚</h2></div><span className="section-heading-row__hint">约 2 分钟</span></div>
          <label className="form-label" htmlFor="wb-question">审查问题</label>
          <input id="wb-question" className="workbench__input" value={question} onChange={(event) => onQuestionChange(event.target.value)} placeholder="例如：这个业务是否需要数据出境安全评估？" disabled={loading} />
          <label className="form-label" htmlFor="wb-material">待审查材料</label>
          <div className="material-toolbar">
            <span>{selectedFile ? `已选择：${selectedFile.name}` : '粘贴项目说明、数据流、供应商信息或合同片段'}</span>
            <input ref={fileInputRef} type="file" accept=".txt,.md,.markdown,.pdf,.docx,.html,.htm,.json" onChange={handleFileChange} hidden />
            <button type="button" className="btn-secondary" onClick={() => fileInputRef.current?.click()} disabled={loading}>上传材料</button>
          </div>
          {fileError ? <div className="form-error">{fileError}</div> : null}
          <textarea id="wb-material" className="workbench__textarea" value={material} onChange={(event) => onMaterialChange(event.target.value)} placeholder="描述业务如何收集、使用和向境外提供数据……" disabled={loading} rows={10} />
          <div className="intake-card__footer"><span>材料将作为案件记录保存，并与审查结果、证据和整改动作关联。</span><button type="button" className="btn-primary" disabled={!question.trim() || (!material.trim() && !selectedFile)} onClick={() => setStep(2)}>继续确认案件要素 →</button></div>
        </section>
      ) : (
        <section className="card intake-card">
          <div className="section-heading-row"><div><div className="report-kicker">02 / 关键要素</div><h2>确认影响判断的事实</h2></div><button type="button" className="btn-link" onClick={() => setStep(1)}>← 返回材料</button></div>
          <div className="intake-grid">
            <label className="form-field form-field--wide"><span>业务活动</span><input value={intake.business_activity} onChange={(event) => updateIntake(intake, onIntakeChange, 'business_activity', event.target.value)} placeholder="例如：推荐系统、客服平台、人力资源管理" /></label>
            <label className="form-field"><span>数据类型</span><input value={intake.data_types.join('、')} onChange={(event) => updateIntake(intake, onIntakeChange, 'data_types', event.target.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean))} placeholder="手机号、定位信息" /></label>
            <label className="form-field"><span>境外接收方</span><input value={intake.overseas_recipient} onChange={(event) => updateIntake(intake, onIntakeChange, 'overseas_recipient', event.target.value)} placeholder="公司/供应商名称" /></label>
            <label className="form-field"><span>目的地</span><input value={intake.destination_region} onChange={(event) => updateIntake(intake, onIntakeChange, 'destination_region', event.target.value)} placeholder="国家或地区" /></label>
            <label className="form-field"><span>非敏感个人信息数量</span><input value={intake.annual_non_sensitive_count} onChange={(event) => updateIntake(intake, onIntakeChange, 'annual_non_sensitive_count', event.target.value)} placeholder="年度估算区间" /></label>
            <label className="form-field"><span>敏感个人信息数量</span><input value={intake.annual_sensitive_count} onChange={(event) => updateIntake(intake, onIntakeChange, 'annual_sensitive_count', event.target.value)} placeholder="年度估算区间" /></label>
            <label className="form-field"><span>重要数据识别状态</span><select value={intake.important_data_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'important_data_status', event.target.value as CaseIntake['important_data_status'])}><option value="unknown">尚未判断</option><option value="not_important">已确认不涉及</option><option value="important">已确认涉及</option><option value="under_review">正在评估</option></select></label>
            <label className="form-field"><span>关基运营者状态</span><select value={intake.ciio_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'ciio_status', event.target.value as CaseIntake['ciio_status'])}><option value="unknown">尚未判断</option><option value="not_ciio">已确认不是</option><option value="ciio">已确认是</option><option value="under_review">正在评估</option></select></label>
            <label className="form-field"><span>合同/标准合同状态</span><input value={intake.contract_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'contract_status', event.target.value)} placeholder="未签署、已签署、待法务确认" /></label>
            <label className="form-field"><span>拟采用的出境路径</span><input value={intake.transfer_mechanism} onChange={(event) => updateIntake(intake, onIntakeChange, 'transfer_mechanism', event.target.value)} placeholder="评估、标准合同、认证或待判断" /></label>
            <label className="form-field form-field--wide"><span>处理目的与补充说明</span><textarea value={`${intake.processing_purpose}${intake.notes ? `\n${intake.notes}` : ''}`} onChange={(event) => updateIntake(intake, onIntakeChange, 'processing_purpose', event.target.value)} placeholder="补充业务目的、例外情况和当前已知限制" rows={3} /></label>
          </div>
          <div className="intake-confirmation"><label><input type="checkbox" checked={intake.cross_border_transfer === true} onChange={(event) => updateIntake(intake, onIntakeChange, 'cross_border_transfer', event.target.checked)} /> <strong>我确认材料涉及向境外提供数据</strong></label><span>未确认的事实会在审查结果中显示为待补充，不会由系统擅自推断。</span></div>
          <details className="workbench-advanced"><summary>审查运行设置</summary><div className="workbench-advanced__body"><label className="form-field"><span>审查深度</span><select value={reviewMode} onChange={(event) => onReviewModeChange(event.target.value as 'llm' | 'multi_agent')}><option value="llm">标准审查</option><option value="multi_agent">深入审查</option></select></label><label className="intake-confirmation"><input type="checkbox" checked={rerankMode === 'embedding'} onChange={(event) => onRerankModeChange(event.target.checked ? 'embedding' : 'off')} /> 启用增强依据排序</label></div></details>
          <div className="intake-card__footer"><span>输出是受控决策辅助，最终结论需要合规审核人确认。</span><button type="button" className="btn-primary" disabled={!canSubmit} onClick={submit}>{loading ? '正在提交案件…' : editingCaseId ? '保存补充并重新提交' : '创建案件并提交审查'}</button></div>
        </section>
      )}

      <section className="guardrail-strip"><span className="guardrail-strip__mark">⌁</span><div><strong>证据优先，明确边界</strong><p>系统会把关键结论连接到法规依据；如果材料不足或检索不到主法源，会停留在待补充状态。</p></div></section>
    </div>
  );
}
