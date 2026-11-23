"""What a legal source's metadata must be consistent with, checked wherever a source is published.

The document type, the authority, the citation role and the region describe
one instrument and decide its citability, so they must agree with each other:
a FAQ is not a primary legal basis, a provincial list is not nationwide, an
enacted norm has an issuing body and a date of entry into force. Each rule
below states one such agreement. Ingestion, metadata edits and reviewer
approval all refuse a record that breaks one, and a test holds the published
corpus to the same rules, so a wrong classification cannot reach retrieval
quietly again.
"""

from __future__ import annotations

import re
from datetime import date

from law_agent.data.schemas import SourceRecord

NORM_DOC_TYPES = frozenset({"law", "regulation", "judicial_interpretation"})
# The authorities each document type can have.
AUTHORITIES_BY_DOC_TYPE: dict[str, frozenset[str]] = {
    "law": frozenset({"national_law"}),
    "regulation": frozenset({"administrative_regulation", "departmental_rule", "local_regulation"}),
    "judicial_interpretation": frozenset({"judicial_interpretation"}),
    "policy": frozenset({"ministry_policy", "local_normative_document"}),
    "guideline": frozenset({"ministry_policy", "local_normative_document", "public_interpretation",
                            "technical_guidance"}),
    "standard": frozenset({"national_standard"}),
    "faq": frozenset({"public_interpretation"}),
    "contract": frozenset({"ministry_policy"}),
}
ROLE_BY_DOC_TYPE: dict[str, frozenset[str]] = {
    "faq": frozenset({"interpretation_auxiliary"}),
    "standard": frozenset({"implementation_reference"}),
    "contract": frozenset({"implementation_reference"}),
}
# An issuing body names an organ, not a place: "中国（天津）自由贸易试验区" is where a list applies.
_ORGAN_RE = re.compile(r"(?:部|委员会|委|局|署|院|办公室|网信办|总局|银行|政府|人民代表大会常务委员会|秘书处|部门|管委会)")
_PLACE_ONLY_RE = re.compile(r"(?:自由贸易试验区|自由贸易港|示范区|片区)$")


def metadata_problems(source: SourceRecord, *, today: date | None = None) -> list[str]:
    """Every rule ``source`` breaks, in words a reviewer can act on. Internal policies are not checked."""

    if source.library_kind != "legal":
        return []
    today = today or date.today()
    problems: list[str] = []
    allowed = AUTHORITIES_BY_DOC_TYPE.get(source.doc_type)
    if allowed is None:
        problems.append(f"法律资料库不收 {source.doc_type} 类型")
    elif source.authority not in allowed:
        problems.append(f"{source.doc_type} 的效力层级只能是 {'/'.join(sorted(allowed))}，不能是 {source.authority}")
    if source.doc_type in ROLE_BY_DOC_TYPE and source.citation_role not in ROLE_BY_DOC_TYPE[source.doc_type]:
        problems.append(f"{source.doc_type} 的引用角色只能是 {'/'.join(sorted(ROLE_BY_DOC_TYPE[source.doc_type]))}")
    if source.citation_role == "primary_legal_basis" and (
            source.doc_type not in NORM_DOC_TYPES or source.authority == "local_regulation"):
        problems.append("只有全国适用的法律、行政法规、部门规章、司法解释可作为主要法规依据")
    if source.authority == "local_regulation" and source.citation_role != "conditional_local_basis":
        problems.append("地方性法规的引用角色应为地方性条件依据")
    if source.citation_role == "conditional_local_basis" and source.applicable_region == "CN":
        problems.append("地方性条件依据必须写明适用地区")
    if (source.applicable_region != "CN" and source.doc_type in NORM_DOC_TYPES | {"policy"}
            and source.citation_role != "conditional_local_basis"):
        problems.append("只适用于特定地区的法规或规范性文件，引用角色应为地方性条件依据")
    if source.authority == "local_normative_document" and source.applicable_region == "CN":
        problems.append("地方规范性文件必须写明适用地区")
    if source.doc_type in NORM_DOC_TYPES:
        problems += [f"正式法规缺少{label}" for name, label in (
            ("effective_date", "生效日期"), ("issuing_body", "发布机关"))
            if not getattr(source, name)]
    elif not (source.publish_date or source.effective_date):
        problems.append("缺少发布日期：它是这份资料唯一的时间依据")
    if source.issuing_body and (_PLACE_ONLY_RE.search(source.issuing_body) or not _ORGAN_RE.search(source.issuing_body)):
        problems.append(f"发布机关“{source.issuing_body}”不是机关名称")
    if not source.issuing_body:
        problems.append("缺少发布机关")
    if "_" in source.title:
        problems.append("标题带有网页后缀")
    if source.effective_date and source.law_status in {"effective", "not_yet_effective"}:
        expected = "effective" if date.fromisoformat(source.effective_date) <= today else "not_yet_effective"
        if source.law_status != expected:
            problems.append(f"效力状态应为 {expected}（生效日期 {source.effective_date}）")
    return problems


def require_consistent_metadata(source: SourceRecord) -> None:
    """Refuse to publish a record whose metadata contradicts itself."""

    if problems := metadata_problems(source):
        raise ValueError(f"元数据不一致，未入库 {source.source_id}：" + "；".join(problems))
