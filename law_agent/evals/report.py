"""Reports with an honest reading guide: counts first, small samples as n/N.

Legal and execution outcomes are reported separately; splits, review status
and answer-leak scenarios are never pooled into one headline number.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from law_agent.evals.bank.schema import Scenario
from law_agent.evals.grading import Grade
from law_agent.evals.runs import wilson_interval

LEGAL_ORDER = ("correct_definite", "correct_conditional", "correct_abstain",
               "wrong_definite", "other_legal_failure", "unevaluated")
EXECUTION_ORDER = ("delivered", "budget_exhausted", "runtime_failure", "blocked", "unanswered_gate")

READING_GUIDE = [
    "## 口径",
    "",
    "- 法律结果与执行结果分开统计；`unevaluated`（judge 或模型服务故障）不计入分母。",
    "- 危险错误率 = `wrong_definite` / 已评定场景数：给出了违反判断边界的确定结论。",
    "- 括号内为 95% Wilson 区间；样本小时区间很宽，不应只看点估计。",
    "- dev、test、field 分开报告；draft 标签的场景未经人工审定，其通过不能称为法律准确率。",
    "- 语料中存在直接答案（政策问答）的场景单独列出。",
]


def _rate(k: int, n: int) -> str:
    if n == 0:
        return "—"
    interval = wilson_interval(k, n)
    return f"{k}/{n}（{k / n:.0%}，{interval[0]:.0%}–{interval[1]:.0%}）"


def summarize(grades: Sequence[Grade]) -> dict[str, Any]:
    legal = Counter(g.legal for g in grades)
    evaluated = sum(v for k, v in legal.items() if k != "unevaluated")
    correct = legal["correct_definite"] + legal["correct_conditional"] + legal["correct_abstain"]
    return {
        "n": len(grades),
        "legal": {k: legal.get(k, 0) for k in LEGAL_ORDER},
        "execution": {k: sum(g.execution == k for g in grades) for k in EXECUTION_ORDER},
        "evaluated": evaluated,
        "correct": correct,
        "wrong_definite": legal["wrong_definite"],
        "failure_categories": dict(Counter(c for g in grades for c in g.failure_categories).most_common()),
    }


def agent_report(grades: Sequence[Grade], scenarios: Sequence[Scenario], manifest: dict[str, Any]) -> tuple[dict, str]:
    by_id = {s.scenario_id: s for s in scenarios}
    arms = sorted({g.arm for g in grades})
    groups: dict[str, Any] = {}

    def keyed(predicate) -> dict[str, Any]:
        return {arm: summarize([g for g in grades if g.arm == arm and predicate(by_id[g.scenario_id])])
                for arm in arms}

    for split in sorted({s.split for s in scenarios}):
        groups[f"split={split}"] = keyed(lambda s, split=split: s.split == split)
    groups["approved_only"] = keyed(lambda s: s.label_status == "approved")
    groups["answer_leak"] = keyed(lambda s: bool(s.answer_leak_sources))
    data = {"manifest": manifest, "groups": groups,
            "per_scenario": [g.model_dump(mode="json") for g in grades]}

    lines = ["# Agent 评测报告", "",
             f"代码：{manifest.get('base_commit', '')[:12]}{'（工作区有未提交改动）' if manifest.get('working_tree_dirty') else ''}",
             f"配置：`{json.dumps(manifest.get('config', {}), ensure_ascii=False)}`", "", *READING_GUIDE, ""]
    for name, per_arm in groups.items():
        if not any(row["n"] for row in per_arm.values()):
            continue
        lines += [f"## {name}", "", "| 系统 | 场景数 | 已评定 | 正确 | 危险错误 | 执行失败 |", "| --- | ---: | ---: | --- | --- | ---: |"]
        for arm, row in per_arm.items():
            failed = row["n"] - row["execution"]["delivered"] - row["execution"]["budget_exhausted"]
            lines.append(f"| {arm} | {row['n']} | {row['evaluated']} | {_rate(row['correct'], row['evaluated'])} | "
                         f"{_rate(row['wrong_definite'], row['evaluated'])} | {failed} |")
        lines.append("")
    lines += ["## 逐场景", "", "| 场景 | 系统 | 执行 | 法律结果 | 失败分类 |", "| --- | --- | --- | --- | --- |"]
    for g in sorted(grades, key=lambda g: (g.scenario_id, g.arm)):
        lines.append(f"| {g.scenario_id} | {g.arm} | {g.execution} | {g.legal} | {', '.join(g.failure_categories) or '—'} |")
    return data, "\n".join(lines) + "\n"


def retrieval_report(aggregate: dict[str, Any], manifest: dict[str, Any]) -> str:
    def fmt(values: dict[str, Any], key: str) -> str:
        value = values.get(key)
        return "—" if value is None else f"{value:.1%}"

    def row(name: str, v: dict[str, Any]) -> str:
        return (f"| {name} | {v['n']} | {v['must_cite_n']} | {fmt(v, 'must_cite_source')} | "
                f"{fmt(v, 'must_cite_article')} | {fmt(v, 'must_cite_discoverable')} | "
                f"{fmt(v, 'must_retrieve_discoverable')} | {fmt(v, 'query_target@3')} | "
                f"{fmt(v, 'query_target@8')} | {v['excluded_hit_scenarios']} |")

    header = ["| 范围 | 场景 | 必引条款 | 来源召回 | 条款召回 | 可发现率 | 必检来源可发现 | 单查询目标@3 | 单查询目标@8 | 命中排除条款的场景 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    lines = ["# 检索评测报告", "", f"代码：{manifest.get('base_commit', '')[:12]}", "",
             ("固定查询直接调用生产检索 `ComplianceAgentTools.search`（一批查询共返回 10 条正文）。"
              "来源召回：正文里有该法规；条款召回：该条作为正文返回；可发现率：该条已取得，或在检索结果列表里以预览出现。"
              "单查询目标@k：每条查询对准的条款在它自己的融合排序里进入前 k。"
              "排除条款指该审查时点已失效、被取代或地域/行业不适用的条文。"), "",
             *header, row("全部", aggregate["overall"]), row("不含直接答案场景", aggregate["without_answer_leak"])]
    lines += [row(f"split={k}", v) for k, v in aggregate["by_split"].items()]
    lines += ["", "## 按判断点", "", *header]
    lines += [row(k, v) for k, v in aggregate["by_judgment_point"].items()]
    return "\n".join(lines) + "\n"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
