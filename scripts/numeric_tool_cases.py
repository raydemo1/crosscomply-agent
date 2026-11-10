"""Synthetic fixtures for the review Agent numerical-tool pilot."""

import random

from law_agent.review.evalset.agent_schemas import AgentCase, AgentRubric

LAW = "促进和规范数据跨境流动规定"
SOURCE = "https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm"
DATE = "2024-03-23"
EXEMPT = "免予三种出境机制"
CONTRACT = "订立个人信息出境标准合同或通过个人信息保护认证"
ASSESSMENT = "申报数据出境安全评估"
UNKNOWN = "累计去重出境人数未知，只能条件化判断或要求补充"

CONTEXT = (
    "已确认：北京企业，非关键信息基础设施运营者、非自贸区，向境外服务商提供在境内收集的数据。"
    "材料已排除境外采集回传、个人合同所必需、人力资源和紧急救助等豁免。"
    "除材料另有说明，无重要数据、无敏感个人信息。仅判断截至2024年3月23日的出境机制。"
)


def ledger(seed, base, target, *, sensitive=False, crosscheck=False, quoted=False):
    rng = random.Random(seed)
    labels = rng.sample(range(1000, 9999), 145)
    eligible = labels[: target - base]
    blocked = labels[100:125]
    earlier = labels[125:145]
    rows = [(f"0{i}", "03-20", "OK") for i in eligible]
    rows.extend((f"0{i}", "03-21", "OK") for i in rng.choices(eligible, k=61))
    rows.extend((f"0{i}", "03-21", "FAILED" if not crosscheck else "OK") for i in blocked)
    rows.extend((f"0{i}", "2023-12-30", "OK") for i in earlier)
    rng.shuffle(rows)
    if quoted:
        lines = [
            "主体,日期,状态,备注",
            *[f'{i},{day},{status},"渠道,已复核"' for i, day, status in rows],
        ]
    else:
        lines = ["主体\t日期\t状态", *["\t".join(row) for row in rows]]
    assert (
        len(
            {
                i
                for i, day, status in rows
                if day.startswith("03-")
                and status == "OK"
                and (not crosscheck or int(i) not in blocked)
            }
        )
        + base
        == target
    )
    category = "敏感" if sensitive else "普通"
    extra = "另无普通个人信息出境。" if sensitive else ""
    if crosscheck:
        extra += (
            "下方撤销表中的主体全部排除。\n【撤销表】\n主体\n"
            + "\n".join(f"0{i}" for i in blocked)
            + "\n"
        )
    return (
        f"本年主台账已经确认有{base}名{category}个人信息主体完成出境。下表是与主台账主体完全不重叠的"
        f"补录日志，都是{category}个人信息，03开头的日期属于2024年，状态OK才实际出境。"
        f"主体字段为保留前导零的自然人编号，可跨行去重。{extra}\n【补录日志】\n" + "\n".join(lines)
    )


def cases():
    definitions = [
        (
            "units_exact",
            "arithmetic",
            "三个互不重叠批次分别为9.375万人、0.6249万人、1人，均为年内实际出境的普通个人信息去重人数。",
            100000,
            CONTRACT,
            "三个批次换算为人后求和",
        ),
        (
            "subtract_below",
            "arithmetic",
            "已核实年内普通个人信息出境去重台账为103418人，其中3419人原登记为已出境，后查明实际未出境，应全部剔除。",
            99999,
            EXEMPT,
            "从年内台账剔除3419名实际未出境者",
        ),
        (
            "decimal_sensitive",
            "arithmetic",
            "年内敏感个人信息实际出境分三组，人数互不重叠，分别为0.8473万人、1.402千人、125人；普通个人信息出境人数为零。",
            10000,
            ASSESSMENT,
            "三组敏感人数按统一单位求和",
        ),
        (
            "dedup_below",
            "table",
            ledger(155, 99943, 99999),
            99999,
            EXEMPT,
            "主台账加上本年OK日志按主体去重后的56人",
        ),
        (
            "dedup_exact",
            "table",
            ledger(222, 99938, 100000),
            100000,
            CONTRACT,
            "主台账加上本年OK日志按主体去重后的62人",
        ),
        (
            "cancel_join",
            "table",
            ledger(328, 999940, 1000000, crosscheck=True),
            1000000,
            ASSESSMENT,
            "主台账加上本年OK日志去重并排除撤销表后的60人",
        ),
        (
            "quoted_csv",
            "table",
            ledger(431, 9948, 9999, sensitive=True, quoted=True),
            9999,
            CONTRACT,
            "敏感主台账加上本年OK日志按主体去重后的51人",
        ),
        (
            "id_ranges",
            "table",
            "年内普通个人信息出境共涉及甲、乙、丙三份封闭清单，所有编号都是同一自然人主键。区间包含两端，状态成功才计入。\n清单 | 编号区间 | 状态\n甲 | 1–72000 | 成功\n乙 | 59997–93567 | 成功\n丙 | 91779–99990 | 成功\n丙 | 99994–100003 | 成功\n甲 | 99991–99993 | 失败",
            100000,
            CONTRACT,
            "成功区间合并去重，排除三名仅出现在失败行的主体",
        ),
        (
            "missing_overlap",
            "control",
            "内部业务日均处理50万人。三个月各自有39000、37500、32000名普通个人信息主体实际出境，月内已去重，但跨月重复主体数未知，缺少主体明细。",
            None,
            UNKNOWN,
            "区分日均处理量和年内累计，保留跨月重叠未知",
        ),
        (
            "important_override",
            "control",
            "主管部门已确认本次提供的地理测绘数据为重要数据。年内本项目普通与敏感个人信息出境人数均为0。该事实优先于材料通用说明。",
            0,
            ASSESSMENT,
            "重要数据单独触发安全评估，人数已给定",
        ),
    ]
    for case_id, group, material, expected, outcome, method in definitions:
        case = AgentCase(
            case_id=case_id,
            material_text=CONTEXT + "\n\n" + material,
            intake={"as_of_date": DATE},
            question=f"依据《{LAW}》判断适用的出境机制。请在结论中列明截至2024年3月23日的相关累计去重出境人数、单位和材料计算口径；如无法确定，请说明缺口与对机制的影响。",
            rubric=AgentRubric(should_abstain=None, allowed_judgments=[outcome], notes=method),
            reference_basis=[SOURCE],
        )
        yield case, {"group": group, "count": expected, "outcome": outcome, "method": method}
