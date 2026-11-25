"""What an arm is: a system under test that turns a scenario into a delivery."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from law_agent.evals.bank.schema import Scenario, render_materials
from law_agent.evals.delivery import Delivery
from law_agent.llm.openai_compatible import ChatMessage


@dataclass
class ArmContext:
    chunks_path: Path
    event: Callable[[str, object], None] = field(default=lambda kind, value: None)


class Arm(Protocol):
    name: str

    def config(self) -> dict[str, Any]: ...

    def run(self, scenario: Scenario, ctx: ArmContext) -> Delivery: ...


ANSWER_SYSTEM = """你是企业数据出境合规审查顾问。根据申请人确认的事实和业务材料回答审查问题。
按审查时点有效的中国法律法规作答；给出法律依据时写明法律文件全称和条号。
事实不足以下确定结论时，说明缺口并给出条件分支，不要编造事实。只输出要求的 JSON。"""


def scenario_prompt(scenario: Scenario) -> str:
    return "\n".join([
        f"【审查时点】{scenario.as_of_date.isoformat()}",
        f"【审查问题】{scenario.question}",
        "【申请人确认的事实】" + json.dumps(scenario.intake, ensure_ascii=False),
        "【业务材料】", render_materials(scenario),
    ])


def answer_messages(scenario: Scenario, extra: str = "") -> list[ChatMessage]:
    return [ChatMessage(role="system", content=ANSWER_SYSTEM + extra),
            ChatMessage(role="user", content=scenario_prompt(scenario))]
