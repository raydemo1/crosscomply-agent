"""Load the scenario bank and materialize material bundles as real files."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from law_agent.evals.bank.schema import Persona, Scenario, Split

BANK_DIR = Path(__file__).with_name("data")
# Field scenarios derive from real (de-identified) cases and stay out of Git.
FIELD_DIR = Path("data/evals/field")


def load_personas(bank_dir: Path = BANK_DIR) -> dict[str, Persona]:
    rows = json.loads((bank_dir / "personas.json").read_text(encoding="utf-8"))
    personas = [Persona.model_validate(row) for row in rows]
    if len({p.persona_id for p in personas}) != len(personas):
        raise ValueError("duplicate persona_id")
    return {p.persona_id: p for p in personas}


def _read_dir(directory: Path) -> list[Scenario]:
    scenarios = []
    for path in sorted(directory.glob("*.json")):
        scenario = Scenario.model_validate_json(path.read_text(encoding="utf-8"))
        if scenario.scenario_id != path.stem:
            raise ValueError(f"{path.name}: 文件名必须等于 scenario_id")
        scenarios.append(scenario)
    return scenarios


def load_scenarios(
    bank_dir: Path = BANK_DIR, *, field_dir: Path | None = FIELD_DIR,
    splits: Iterable[Split] | None = None, ids: Iterable[str] | None = None,
) -> list[Scenario]:
    scenarios = _read_dir(bank_dir / "scenarios")
    if field_dir is not None and field_dir.is_dir():
        scenarios += _read_dir(field_dir)
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.scenario_id in seen:
            raise ValueError(f"duplicate scenario_id: {scenario.scenario_id}")
        seen.add(scenario.scenario_id)
    if splits is not None:
        wanted = set(splits)
        scenarios = [s for s in scenarios if s.split in wanted]
    if ids is not None:
        wanted_ids = set(ids)
        unknown = wanted_ids - {s.scenario_id for s in scenarios}
        if unknown:
            raise ValueError(f"unknown scenario ids: {sorted(unknown)}")
        scenarios = [s for s in scenarios if s.scenario_id in wanted_ids]
    return scenarios


def write_scenario(scenario: Scenario, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario.scenario_id}.json"
    text = json.dumps(scenario.model_dump(mode="json", exclude_defaults=False), ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def materialize(scenario: Scenario, directory: Path) -> list[Path]:
    """Write the bundle as uploadable files: text as UTF-8, sheets as XLSX."""

    from openpyxl import Workbook

    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for material in scenario.materials:
        path = directory / material.filename
        if material.sheets is not None:
            workbook = Workbook()
            workbook.remove(workbook.active)
            for table in material.sheets:
                sheet = workbook.create_sheet(table.name)
                for row in table.rows:
                    sheet.append(row)
            workbook.save(path)
        else:
            path.write_text(material.text or "", encoding="utf-8")
        paths.append(path)
    return paths
