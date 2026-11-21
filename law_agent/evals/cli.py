"""Command line: ``python -m law_agent.evals <command>``.

``check``, ``review-package``, ``record-review``, ``lock-test``,
``export-prompts`` and ``report`` never call a model. ``retrieval`` calls the
embedding service only; ``qa`` and ``agent`` call paid models.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH


def _scenarios(args: argparse.Namespace):
    from law_agent.evals.bank.loader import load_scenarios

    return load_scenarios(splits=args.split or None, ids=args.scenario or None)


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--split", action="append", choices=["dev", "test", "field"],
                        help="可重复；不指定时选择全部")
    parser.add_argument("--scenario", action="append", help="只运行指定场景，可重复")


def cmd_check(args: argparse.Namespace) -> int:
    from law_agent.evals.bank.checks import CorpusIndex, check_bank, coverage
    from law_agent.evals.bank.loader import load_personas, load_scenarios
    from law_agent.review.retrieval.corpus import load_corpus

    scenarios = load_scenarios()
    corpus = None if args.no_corpus else CorpusIndex(load_corpus(args.chunks))
    errors = check_bank(scenarios, corpus, set(load_personas()))
    print(f"scenarios={len(scenarios)}")
    for name, counter in coverage(scenarios).items():
        print(f"[{name}] " + ", ".join(f"{key}={value}" for key, value in sorted(counter.items())))
    for error in errors:
        print("ERROR", error)
    return 1 if errors else 0


def _rerank_manifest(mode: str | None) -> dict[str, object]:
    from law_agent.config import load_rerank_config

    config = load_rerank_config(mode=mode)
    return {"rerank_mode": config.mode, "rerank_model": config.model, "rerank_blend_weight": config.blend_weight}


def cmd_retrieval(args: argparse.Namespace) -> int:
    from law_agent.evals.report import retrieval_report, write
    from law_agent.evals.retrieval import production_search, run_retrieval
    from law_agent.evals.runs import RunStore

    scenarios = [s for s in _scenarios(args) if s.retrieval is not None]
    store = RunStore(args.output_dir)
    manifest = store.write_manifest(kind="retrieval", scenarios=scenarios, chunks_path=args.chunks,
                                    config=_rerank_manifest(args.rerank_mode))
    results, aggregate = run_retrieval(scenarios, production_search(args.chunks, rerank_mode=args.rerank_mode))
    for row in results:
        store.save(store.case_dir("retrieval", row.scenario_id), "result.json", row.model_dump(mode="json"))
    store.save(store.root, "aggregate.json", aggregate)
    write(store.root / "report.md", retrieval_report(aggregate, manifest))
    store.finish({"kind": "retrieval", "scenarios": len(results)})
    print(json.dumps(aggregate["overall"], ensure_ascii=False))
    return 0


def cmd_qa(args: argparse.Namespace) -> int:
    from law_agent.evals.qa_eval import qa_report, run_qa_eval
    from law_agent.evals.report import write
    from law_agent.evals.runs import RunStore

    scenarios = [s for s in _scenarios(args) if s.retrieval is not None]
    store = RunStore(args.output_dir)
    manifest = store.write_manifest(kind="qa", scenarios=scenarios, chunks_path=args.chunks,
                                    config={**_rerank_manifest(None), "workers": args.workers})
    runs, aggregate = run_qa_eval(
        scenarios, chunks_path=args.chunks, workers=args.workers,
        on_result=lambda run: store.save(store.case_dir("qa", run.scenario_id), "result.json",
                                         run.model_dump(mode="json")),
    )
    store.save(store.root, "aggregate.json", aggregate)
    write(store.root / "report.md", qa_report(aggregate, manifest))
    store.finish({"kind": "qa", "scenarios": len(runs)})
    print(json.dumps(aggregate["overall"], ensure_ascii=False))
    return 0


def _arms(args: argparse.Namespace) -> list:
    from law_agent.config import require_llm_config
    from law_agent.evals.arms import CrossComplyArm, DirectArm, ImportedArm, WebAgentArm

    model = args.model or require_llm_config().model
    factories = {
        "crosscomply": lambda: CrossComplyArm(model_id=model, rerank_mode=args.rerank_mode,
                                              skip_verifier=args.verifier == "skip"),
        "direct": lambda: DirectArm(model_id=model),
        "web": lambda: WebAgentArm(model_id=model),
    }
    arms = [factories[name]() for name in args.arm]
    for spec in args.imported or []:
        name, _, directory = spec.partition("=")
        arms.append(ImportedArm(name=name, directory=Path(directory)))
    return arms


def cmd_agent(args: argparse.Namespace) -> int:
    from law_agent.config import require_llm_config
    from law_agent.evals.agent_eval import run_agent_eval
    from law_agent.evals.grading import StructuredJudge
    from law_agent.evals.report import agent_report, write
    from law_agent.evals.runs import RunStore

    scenarios = _scenarios(args)
    if args.without_portfolio:
        scenarios = [s.model_copy(update={"portfolio": None}) for s in scenarios]
    arms = _arms(args)
    judge_model = None if args.no_judge else (args.judge_model or args.model or require_llm_config().model)
    judge = None if args.no_judge else StructuredJudge(model_id=judge_model)
    store = RunStore(args.output_dir)
    grades = run_agent_eval(scenarios, arms, store=store, chunks_path=args.chunks, judge=judge,
                            judge_model=judge_model, workers=args.workers)
    manifest = json.loads((store.root / "manifest.json").read_text(encoding="utf-8"))
    data, markdown = agent_report(grades, scenarios, manifest)
    store.save(store.root, "report.json", data)
    write(store.root / "report.md", markdown)
    print(markdown)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from law_agent.evals.bank.loader import load_scenarios
    from law_agent.evals.grading import Grade
    from law_agent.evals.report import agent_report, write

    manifest = json.loads((args.run_dir / "manifest.json").read_text(encoding="utf-8"))
    grades = [Grade.model_validate_json(p.read_text(encoding="utf-8")) for p in sorted(args.run_dir.glob("*/*/grade.json"))]
    scenarios = load_scenarios(ids=manifest["scenarios"])
    _data, markdown = agent_report(grades, scenarios, manifest)
    write(args.run_dir / "report.md", markdown)
    print(markdown)
    return 0


def cmd_review_package(args: argparse.Namespace) -> int:
    from law_agent.evals.review import export_review_package

    manifest = export_review_package(_scenarios(args), args.output_dir)
    print(json.dumps({"output_dir": str(args.output_dir), "counts": manifest["counts"]}, ensure_ascii=False))
    return 0


def cmd_record_review(args: argparse.Namespace) -> int:
    from law_agent.evals.bank.loader import load_scenarios
    from law_agent.evals.review import record_review

    print(f"recorded {record_review(args.review_file, load_scenarios(field_dir=None))} decision(s)")
    return 0


def cmd_lock_test(args: argparse.Namespace) -> int:
    from law_agent.evals.bank.loader import load_scenarios
    from law_agent.evals.review import lock_test_split

    print(f"locked {lock_test_split(load_scenarios(field_dir=None), allow_draft=args.allow_draft)} test scenario(s)")
    return 0


def cmd_export_prompts(args: argparse.Namespace) -> int:
    from law_agent.evals.arms.baselines import export_prompts

    print(f"wrote {export_prompts([s for s in _scenarios(args) if s.agent_rubric], args.output_dir)} prompt(s)")
    return 0


def cmd_field_export(args: argparse.Namespace) -> int:
    from law_agent.evals.field import main as field_main

    forwarded = [*(["--output-dir", str(args.output_dir)] if args.output_dir else []),
                 *(["--corpus", str(args.corpus)] if args.corpus else []),
                 *(["--dry-run"] if args.dry_run else [])]
    return field_main(forwarded)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m law_agent.evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="场景库一致性检查与覆盖统计（不调用模型）")
    check.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS_PATH)
    check.add_argument("--no-corpus", action="store_true", help="跳过需要语料的检查")
    check.set_defaults(func=cmd_check)

    retrieval = sub.add_parser("retrieval", help="固定查询走生产检索，计算条款级召回")
    _add_selection(retrieval)
    retrieval.add_argument("--output-dir", type=Path, required=True)
    retrieval.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS_PATH)
    retrieval.add_argument("--rerank-mode", choices=["off", "cross_encoder"], default=None,
                       help="覆盖部署配置 RERANK_MODE，用于对照实验")
    retrieval.set_defaults(func=cmd_retrieval)

    qa = sub.add_parser("qa", help="生产问答 Agent 自主多轮检索并作答，按条款标签计证据召回与引用（调用付费模型）")
    _add_selection(qa)
    qa.add_argument("--output-dir", type=Path, required=True)
    qa.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS_PATH)
    qa.add_argument("--workers", type=int, default=4)
    qa.set_defaults(func=cmd_qa)

    agent = sub.add_parser("agent", help="被测系统 × 场景，统一评分（调用付费模型）")
    _add_selection(agent)
    agent.add_argument("--arm", action="append", choices=["crosscomply", "direct", "web"], default=None)
    agent.add_argument("--import", dest="imported", action="append", metavar="NAME=DIR",
                       help="导入外部系统的答案目录，作为一个被测系统")
    agent.add_argument("--model")
    agent.add_argument("--judge-model")
    agent.add_argument("--no-judge", action="store_true")
    agent.add_argument("--without-portfolio", action="store_true", help="去掉同主体累计台账，做对照")
    agent.add_argument("--verifier", choices=["skip", "on"], default="skip",
                       help="skip（默认）：跳过独立语义校验，直接评 Agent 首次生成的报告；on：走完整生产链路")
    agent.add_argument("--workers", type=int, default=1)
    agent.add_argument("--output-dir", type=Path, required=True)
    agent.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS_PATH)
    agent.add_argument("--rerank-mode", choices=["off", "cross_encoder"], default=None,
                       help="覆盖部署配置 RERANK_MODE，用于对照实验")
    agent.set_defaults(func=cmd_agent)

    report = sub.add_parser("report", help="从运行目录重新生成 Agent 报告")
    report.add_argument("run_dir", type=Path)
    report.set_defaults(func=cmd_report)

    package = sub.add_parser("review-package", help="导出人工审定包")
    _add_selection(package)
    package.add_argument("--output-dir", type=Path, required=True)
    package.set_defaults(func=cmd_review_package)

    record = sub.add_parser("record-review", help="写入人工填写的审定结果")
    record.add_argument("review_file", type=Path)
    record.set_defaults(func=cmd_record_review)

    lock = sub.add_parser("lock-test", help="锁定 test 集散列")
    lock.add_argument("--allow-draft", action="store_true")
    lock.set_defaults(func=cmd_lock_test)

    prompts = sub.add_parser("export-prompts", help="导出提示词，供人工在商用助手中运行后导入")
    _add_selection(prompts)
    prompts.add_argument("--output-dir", type=Path, required=True)
    prompts.set_defaults(func=cmd_export_prompts)

    field = sub.add_parser("field-export", help="把就绪的现场纠错草稿导出为 field 场景")
    field.add_argument("--output-dir", type=Path, default=None)
    field.add_argument("--corpus", type=Path, default=None)
    field.add_argument("--dry-run", action="store_true")
    field.set_defaults(func=cmd_field_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "arm", "unset") is None:
        args.arm = [] if getattr(args, "imported", None) else ["crosscomply"]
    return args.func(args)
