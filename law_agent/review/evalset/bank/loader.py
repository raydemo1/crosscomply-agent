"""Load the scenario bank and materialize material bundles as real files."""

from __future__ import annotations

import json
from pathlib import Path

from law_agent.review.evalset.bank.schema import Persona, Scenario

BANK_DIR = Path(__file__).with_name("data")


def load_personas(bank_dir: Path = BANK_DIR) -> dict[str, Persona]:
    rows = json.loads((bank_dir / "personas.json").read_text(encoding="utf-8"))
    personas = [Persona.model_validate(row) for row in rows]
    if len({p.persona_id for p in personas}) != len(personas):
        raise ValueError("duplicate persona_id")
    return {p.persona_id: p for p in personas}


def load_scenarios(bank_dir: Path = BANK_DIR) -> list[Scenario]:
    scenarios = []
    for path in sorted((bank_dir / "scenarios").glob("*.json")):
        scenario = Scenario.model_validate_json(path.read_text(encoding="utf-8"))
        if scenario.scenario_id != path.stem:
            raise ValueError(f"{path.name}: 文件名必须等于 scenario_id")
        scenarios.append(scenario)
    return scenarios


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
