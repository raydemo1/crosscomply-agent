"""Evaluate the actual Q&A runtime without manufacturing review reports."""

import hashlib
import json
import time

from law_agent.config import require_agent_llm_config
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.evalset.agent_schemas import JudgeVerdict
from law_agent.review.http.responses import public_payload
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.qa_agent import QAWorker, execute_qa, is_tool_call_text
from law_agent.review.qa_store import InMemoryQAStore, identifier, now
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH

QA_JUDGE_VERSION = "qa-answer-20261020-rule-scope-v2"
QA_JUDGE_PROMPT = """你是问答评测员，只评测给定候选回答，不重新调查。依据材料、业务信息、候选rubric及引用原文，检查法律判断、例外、事实依据和条件披露。
允许有依据的条件性回答；用户陈述可作为归因清楚的前提，不等于独立核实。不要求风险评级、正式报告、先追问、冻结事实、整改或审批。
should_abstain表示缺少依据时不能给无条件个案结论，不禁止解释已有规则或提供有依据的条件分支。
结合完整段落及明确关联的上下文评价，不要求每句话重复全部条件，也不能替答案补造没有表达的例外。引用编号存在不代表结论获支持。
逐项检查来源的主体、地域、行业、版本及条款衔接。特别检查：
- 一般义务是否受其他已给定规则或合法性基础限定；正文虽然提到例外，但在准备清单或总结中仍要求一律履行，也属于实际判断错误。程序机制豁免与具体义务的例外分别判断，不能相互替代。原则性的必要性或相关性要求，不当然支持合同必须逐项记载哪些事项；核对建议或建议留档被升级为法定前提，应对照原文指出。
- 是否前后矛盾，把不同分支混为累积条件、把一个例外说成唯一例外、一般门槛覆盖特别豁免，或用旧版解释、封面支撑新版具体要求。
- 是否把用户已给出的前提重新列为未知，或者把尚未确认当成不成立；区分原始业务信息与回答声称缺少的信息，评价重复确认是否妨碍当前答复或改变结论。
- 超出资料范围时是否真正限制答复。若用户问法定义务，未取得相应规则却用“一般理解”“常见实践”补出适用分类、义务或实施清单，免责声明不能自动消除无依据扩写。来源仅介绍某法不等于支持其具体要求。用户没有要求比较法或多法域分析，却展开其他法域的“平行参考”，应在clarification_quality及reasons中评价偏题程度，不因引用正确、附有免责声明就忽略，也不把单纯偏题直接当成错误法条。
必须对照allowed_judgments、forbidden_judgments、required_exceptions、must_not_assume及should_abstain，不能只看首段结论。逐项检查正文、清单与总结，不能因主结论正确就忽略其他确定性错误。
改变结论或义务范围的错误应影响legal_correctness或exception_coverage；重复确认评入clarification_quality；无依据扩写同时考虑fact_grounding。rubric_violations记录实质违反，reasons引用具体问题原句及理由；轻微表达问题不升级为实质错误。不因没有审查栏目扣分。仅返回schema JSON。
"""


def run_qa_case(case, *, chunks_path=DEFAULT_CHUNKS_PATH, execute=execute_qa, checkpoint_sink=None, judge=None):
    store = InMemoryQAStore()
    conversation = store.create("qa-evaluation")
    material = case.material_text
    if case.intake:
        material += "\n补充业务信息（用户提供，未独立核实）：\n" + json.dumps(case.intake, ensure_ascii=False)
    materials = []
    if material.strip():
        materials.append(store.add_material(conversation["id"], {
            "id": identifier("qam"), "filename": "candidate-material.txt", "parsed_text": material,
            "created_at": now(), "byte_size": len(material.encode("utf-8")),
        })["id"])
    store.enqueue(conversation["id"], question=case.question, material_ids=materials, request_id="candidate-run")

    def recorded_execute(conversation, run, *, checkpoint, **kwargs):
        def recorded_checkpoint(**changes):
            checkpoint(**changes)
            if checkpoint_sink:
                checkpoint_sink(public_payload(changes))
        execute(conversation, run, checkpoint=recorded_checkpoint, **kwargs)

    started = time.monotonic()
    QAWorker(store, chunks_path=chunks_path, execute=recorded_execute).run_once()
    run = store.get(conversation["id"])["runs"][0]
    result = run["result"] or {}
    evidence = {item["chunk_id"]: item for item in run["state"].get("evidence", [])}
    sources = sorted({evidence[c["chunk_id"]]["source_id"] for c in result.get("citations", []) if c.get("chunk_id") in evidence})
    forbidden = {"review_result", "risk_level", "legal_path", "recommended_actions", "issues", "approval", "remediation_plan"}
    checks = {
        "completed": run["status"] == "completed",
        "nonempty_answer": bool(run["answer"].strip()),
        "plain_answer": not is_tool_call_text(run["answer"]),
        "qa_contract": not (forbidden & result.keys()),
        "legal_citation_identity": all(c.get("kind") != "legal" or (
            c.get("chunk_id") in evidence and evidence[c["chunk_id"]]["can_cite_clause"]
        ) for c in result.get("citations", [])),
        "material_membership": all(c["material_version_id"] in materials for c in result.get("material_citations", [])),
    }
    summary = {"case_id": case.case_id, "entry": "qa", "review_status": case.review_status,
               "question": case.question, "input_sha256": hashlib.sha256(material.encode()).hexdigest(),
               "status": run["status"], "latency_ms": round((time.monotonic() - started) * 1000),
               "turns": run["state"].get("turns", 0), "searches": run["state"].get("searches", 0),
               "reads": run["state"].get("reads", 0),
               "checks": checks, "deterministic_pass": all(checks.values()), "cited_source_ids": sources,
               "run": public_payload(run), "judge": None,
               "limits": "Candidate only; contract checks are not legal accuracy. No formal risk or approval scoring."}
    if judge and all(checks.values()):
        summary["judge"] = judge(case, {**result, "answer": run["answer"]})
    return summary


class QAAnswerJudge:
    """Optional evaluation-only judge, never invoked by the Q&A product."""

    def __init__(self):
        config = require_agent_llm_config()
        self.node = StructuredLLMNode(node_name="qa_eval_judge", output_model=JudgeVerdict,
                                      client=OpenAICompatibleClient(config), structured_output_mode="json_object", max_retries=1)
        self.model = config.model

    def __call__(self, case, result):
        rubric = case.rubric.model_dump(include={"must_not_assume", "required_exceptions", "clarification_expectations",
                                                "allowed_judgments", "forbidden_judgments", "should_abstain", "notes"})
        try:
            verdict = self.node.run([
                ChatMessage(role="system", content=QA_JUDGE_PROMPT),
                ChatMessage(role="user", content=json.dumps({"question": case.question, "material": case.material_text,
                            "user_context": case.intake, "rubric": rubric, "reference_basis": case.reference_basis,
                            "answer": result, "schema": JudgeVerdict.model_json_schema()}, ensure_ascii=False)),
            ])
            verdict.judge_model = self.model
            return {**verdict.model_dump(mode="json"), "prompt_version": QA_JUDGE_VERSION}
        except Exception as exc:  # noqa: BLE001 - judge failure is not an answer failure
            return {**JudgeVerdict(error=type(exc).__name__, judge_model=self.model).model_dump(mode="json"),
                    "prompt_version": QA_JUDGE_VERSION}
