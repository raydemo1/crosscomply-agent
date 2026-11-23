"""Read a new source's governed metadata with a model, every field backed by a quote that is checked.

The model reads the opening and closing of the document (title, address,
issuing order, signature, entry into force) and returns each field together
with the sentence it took it from. Nothing it says is trusted on its face: a
quote must occur in the document (or the title, or the address), a date must be
readable from its quote, an issuing body must be named in its quote, and an
enacted norm (law, regulation, judicial interpretation) must quote the form of
enactment that makes it one. A field that fails is kept for the reviewer but
marked unresolved, so it can never publish on its own.

The model may also say it is unsure of a field, and everything it is unsure
of, could not read, or cannot quote goes to the reviewer: no field is filled for
the sake of filling it. When no model is configured or the call fails, the
rule-based inference (:func:`infer_source`) prefills the draft instead, and
such a draft always waits for a reviewer.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, get_args

from pydantic import BaseModel, Field

from law_agent.data.schemas import SourceRecord
from law_agent.kb.source_inference import (
    _URL_DATE_RES,
    AGENCY_ALIASES,
    DATE_RE,
    ORDER_RE,
    ISO_DATE_RE,
    REGION_CODES,
    SourceInference,
    arabic_dates,
    clean_title,
    infer_source,
    instrument_key,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "source-metadata-v5"
HEAD_CHARS = 3500
TAIL_CHARS = 1500
NORM_DOC_TYPES = {"law", "regulation", "judicial_interpretation"}
# What an enacted norm's quote must show: the form of enactment, not just a name.
ENACTMENT_RE = {
    "national_law": re.compile(r"主席令|全国人民代表大会|人大常委会|npc\.gov\.cn"),
    "administrative_regulation": re.compile(r"国务院令|国务院.{0,10}公布|条例"),
    "departmental_rule": ORDER_RE,
    "local_regulation": re.compile(r"人民代表大会常务委员会|人大常委会"),
    "judicial_interpretation": re.compile(r"法释|高检发释字"),
}

SYSTEM = """你是法律资料库的入库审核员。阅读一份新发现的官方文件，判断它的治理元数据。这些元数据决定检索时它能不能被当作条文依据引用，所以要按文件实际的发布形式和效力判断，而不是按标题里的字眼。

只依据给出的标题、网址和正文，不要凭记忆补充文件之外的信息。每个字段给出 quote：从标题、网址或正文中逐字摘取、能支持该字段的一小段原文（不超过80字）。

不必每个字段都填满，拿不准的交给人工审核：
- 文件里看不出来的字段，value 和 quote 填 null。
- 有倾向但拿不准的字段（比如看不到令号、无法确定是部门规章还是规范性文件），value 填你最可能的判断供审核人参考，同时把字段名放进 uncertain 列表，并在 note 里说明拿不准什么。
人工审核会逐项看你标出的字段；一个看似确定、实际是猜的值，比交给人工更糟。

## 怎么判断
先看它是怎么生效的：
- 以主席令公布、由全国人大或其常委会通过 → law / national_law。
- 以国务院令公布 → regulation / administrative_regulation。
- 以部门令（“……令第N号”）公布的办法、规定 → regulation / departmental_rule。没有令号、而是以通知或公告印发的办法、规定，是规范性文件：policy / ministry_policy（地方部门印发的为 local_normative_document）。
- 省、市人大常委会通过的条例 → regulation / local_regulation。
- 最高人民法院、最高人民检察院的解释（法释、高检发释字）→ judicial_interpretation。
再看它是哪类辅助材料：
- 设定规则、条件、清单的规范性文件（通知、公告、意见、管理清单、负面清单、对特定地区适用的实施指引）→ policy。
- 讲怎么办事、怎么落实的操作文件（申报指南、备案指南、合规指引、分类分级指南）→ guideline / ministry_policy；全国网络安全标准化技术委员会（TC260）的《网络安全标准实践指南》等技术文件 → guideline / technical_guidance。
- 国家标准（GB/T、GB）→ standard / national_standard / implementation_reference。
- 答记者问、问答、政策解读 → faq / public_interpretation / interpretation_auxiliary。
- 合同范本、标准合同文本 → contract / ministry_policy / implementation_reference。

## 引用角色与适用范围
- primary_legal_basis：只给全国适用的法律、行政法规、部门规章、司法解释。
- conditional_local_basis：文件本身只在某个地区适用（地方性法规、自贸区清单、只适用于大湾区的文件等），必须同时给出 applicable_region。
- conditional_industry_basis：文件本身只约束某个行业（汽车、金融、证券、工业和信息化领域等）。
- implementation_reference：落地参考，包括全国适用的规范性文件、指南、标准、范本。
- interpretation_auxiliary：问答、解读。
applicable_region 指文件的适用地域，不是发布机关所在地：全国为 CN；地区代码如 CN-BJ、CN-SH、CN-TJ、CN-CQ、CN-ZJ、CN-JS、CN-FJ、CN-GD、CN-GX、CN-HI、CN-GD-SZ（深圳）；粤港澳大湾区内地与香港为 CN-GBA-HK，与澳门为 CN-GBA-MO。

## 机关与日期
- issuing_body：发布机关的全称，是机关不是地方（“中国（天津）自由贸易试验区”是适用地域，发布机关应是印发它的天津市互联网信息办公室等）。多部门联合发布写牵头机关加“等N部门”（数不清写“等部门”）。问答、解读写发布它的机关。国家标准写“国家市场监督管理总局、国家标准化管理委员会”。
- publish_date：文件首次公开的日期，YYYY-MM-DD。优先用页面发布日期或签署、公布日期；国家标准用“发布”日期。page_date_from_search 是检索引擎给的页面日期，只在正文和网址都没有日期时作参考。
- effective_date：施行、实施或生效日期。“自发布之日起施行”时等于发布日期。文本是修正后版本而修改决定的施行日期未写明时，填 null。问答、解读一般没有生效日期，填 null。

## 取值表（只能用这些英文值）
- doc_type：law、regulation、judicial_interpretation、policy、guideline、standard、faq、contract
- authority（效力层级，不是机关名称）：national_law、administrative_regulation、departmental_rule、local_regulation、judicial_interpretation、ministry_policy、local_normative_document、technical_guidance、public_interpretation、national_standard
- citation_role：primary_legal_basis、conditional_local_basis、conditional_industry_basis、implementation_reference、interpretation_auxiliary
- title：文件本身的名称，去掉网站名、令号或公告号前缀、外层书名号、“（全文）”。
quote 要逐字照抄一段连续原文，不要用省略号拼接；全国适用（CN）不需要 quote。

输出 JSON：{"title": {"value": ..., "quote": ...}, "doc_type": {...}, "authority": {...}, "citation_role": {...}, "applicable_region": {...}, "issuing_body": {...}, "publish_date": {...}, "effective_date": {...}, "uncertain": [拿不准的字段名], "note": "拿不准的原因，没有就填 null"}"""


class _Field(BaseModel):
    value: str | None = None
    quote: str | None = None


class _Answer(BaseModel):
    title: _Field = Field(default_factory=_Field)
    doc_type: _Field = Field(default_factory=_Field)
    authority: _Field = Field(default_factory=_Field)
    citation_role: _Field = Field(default_factory=_Field)
    applicable_region: _Field = Field(default_factory=_Field)
    issuing_body: _Field = Field(default_factory=_Field)
    publish_date: _Field = Field(default_factory=_Field)
    effective_date: _Field = Field(default_factory=_Field)


@dataclass(frozen=True)
class ModelReading:
    inference: SourceInference
    answer: dict[str, Any]
    rejected: dict[str, str]  # field -> why its quote did not hold


def _squash(text: str) -> str:
    return re.sub(r"[\s　*#|]+", "", arabic_dates(text))


_VOCABULARY_FIELDS = ("doc_type", "authority", "citation_role")
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")  # "20260427" in an address


def _dates_in(quote: str) -> set[str]:
    quote = arabic_dates(quote)
    found = {m.group(0) for m in ISO_DATE_RE.finditer(quote)}
    for match in DATE_RE.finditer(quote):
        try:
            found.add(date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat())
        except ValueError:
            continue
    for pattern in (*_URL_DATE_RES, _COMPACT_DATE_RE):
        for match in pattern.finditer(quote):
            try:
                found.add(date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat())
            except ValueError:
                continue
    return found


def _check(name: str, value: str | None, quote: str | None, haystack: str, answer: _Answer,
           page_dates: set[str] = frozenset()) -> str | None:
    """Why ``value`` is not supported by ``quote``, or None when it is.

    ``page_dates`` are the dates the page's address and the search result give: a publication date
    read from either needs no quote from the text."""

    if value is None or (name == "applicable_region" and value == "CN"):
        return None  # nationwide is the default: nothing to quote
    if name == "publish_date" and value in page_dates:
        return None
    if not quote:
        return "未给出原文依据"
    position = 0
    for part in filter(None, map(_squash, re.split(r"……|…|\.{3,}", quote))):
        # A quote elided with "……" holds when its pieces occur in order.
        if (found := haystack.find(part, position)) < 0:
            return "依据不在原文中"
        position = found + len(part)
    if name in {"publish_date", "effective_date"}:
        if value in _dates_in(quote):
            return None
        publish = answer.publish_date.value
        if name == "effective_date" and re.search(r"自\s*(?:公布|发布|印发)之日起", quote) and value == publish:
            return None
        return "日期不能从依据中读出"
    if name == "issuing_body":
        lead = re.split(r"等\d+部门|等|、|，|\s", value)[0]
        names = {lead, *(short for short, full in AGENCY_ALIASES.items() if full == lead)}
        # "上海市第十五届人民代表大会常务委员会" names "上海市人民代表大会常务委员会".
        said = re.sub(r"第[一二三四五六七八九十\d]+届", "", _squash(quote))
        return None if any(_squash(item) in said for item in names if item) else "机关名称不在依据中"
    if name == "authority" and value in ENACTMENT_RE:
        return None if ENACTMENT_RE[value].search(quote) else "依据中没有对应的发布形式（令号、国务院令等）"
    return None


def _reading(data: dict[str, Any], *, source_id: str, title: str, text: str, url: str, source_site: str,
             file_format: str, today: date, published_hint: str | None) -> ModelReading:
    """Check every quote of one answer and build the draft record from what holds."""

    page_dates = _dates_in(url) | ({published_hint[:10]} if published_hint else set())
    answer = _Answer.model_validate({key: value for key, value in data.items() if isinstance(value, dict)})
    haystack = _squash(f"{title}\n{url}\n{text}")
    rejected: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for name in _Answer.model_fields:
        field: _Field = getattr(answer, name)
        if (problem := _check(name, field.value, field.quote, haystack, answer, page_dates)) is not None:
            rejected[name] = problem
        elif field.quote:
            evidence[name] = f"原文：“{field.quote}”"

    def value(name: str, default: Any = None) -> Any:
        field: _Field = getattr(answer, name)
        if field.value in (None, ""):
            return default
        allowed = get_args(SourceRecord.model_fields[name].annotation)
        if name in _VOCABULARY_FIELDS and field.value not in allowed:
            rejected[name] = f"“{field.value}”不是有效取值"
            return default
        return field.value

    doc_type = value("doc_type", "guideline")
    citation_role = value("citation_role", "interpretation_auxiliary")
    authority = value("authority", "unknown")
    effective = value("effective_date")
    publish = value("publish_date")
    region = value("applicable_region", "CN")
    if region not in {"CN", "CN-GBA-HK", "CN-GBA-MO", *REGION_CODES.values()}:
        rejected["applicable_region"] = "不是已知地区代码"
        region = "CN"
    for name, raw in (("publish_date", publish), ("effective_date", effective)):
        if raw and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            rejected[name] = "不是 YYYY-MM-DD 日期"
    publish = None if "publish_date" in rejected else publish
    effective = None if "effective_date" in rejected else effective
    law_status = "unknown"
    if effective:
        law_status = "effective" if date.fromisoformat(effective) <= today else "not_yet_effective"
    elif doc_type not in NORM_DOC_TYPES and publish:
        law_status = "effective"
    clean = clean_title(value("title") or title)
    source = SourceRecord(
        source_id=source_id, library_kind="legal", title=clean, source_url=url, source_site=source_site,
        doc_type=doc_type, authority=authority, citation_role=citation_role,
        law_status=law_status, publish_date=publish, effective_date=effective,
        issuing_body=value("issuing_body"), applicable_region=region, instrument_key=instrument_key(clean),
        file_format=file_format, include_in_mvp=True,
    )
    note = data.get("note")
    if isinstance(note, str) and note.strip() and note.strip().lower() != "null":
        evidence["note"] = note.strip()

    # Left to the reviewer: what the model was unsure of, what it could not read, what its quote does not show.
    uncertain = [name for name in data.get("uncertain") or [] if isinstance(name, str) and name in _Answer.model_fields]
    missing = [name for name in ("doc_type", "authority", "citation_role", "issuing_body")
               if getattr(answer, name).value in (None, "")]
    if doc_type in NORM_DOC_TYPES and not effective:
        missing.append("effective_date")
    elif doc_type not in NORM_DOC_TYPES and not (publish or effective):
        missing.append("publish_date")
    unresolved = list(dict.fromkeys([*rejected, *uncertain, *missing]))
    return ModelReading(
        inference=SourceInference(source=source, evidence=evidence, unresolved=unresolved, reader="model"),
        answer=answer.model_dump() | {key: data[key] for key in ("uncertain", "note") if key in data},
        rejected=rejected,
    )


def read_with_model(
    *, client, source_id: str, title: str, text: str, url: str, source_site: str, file_format: str,
    published_hint: str | None = None, today: date | None = None,
) -> ModelReading:
    """``client`` is an :class:`~law_agent.llm.openai_compatible.OpenAICompatibleClient`."""

    from law_agent.llm.openai_compatible import ChatMessage

    today = today or date.today()
    excerpt = text if len(text) <= HEAD_CHARS + TAIL_CHARS else f"{text[:HEAD_CHARS]}\n……（中间略）……\n{text[-TAIL_CHARS:]}"
    prompt = {"title": title, "url": url, "page_date_from_search": published_hint, "text": excerpt}
    data = client.chat_json([ChatMessage(role="system", content=SYSTEM),
                             ChatMessage(role="user", content=json.dumps(prompt, ensure_ascii=False))])
    return _reading(data, source_id=source_id, title=title, text=text, url=url, source_site=source_site,
                    file_format=file_format, today=today, published_hint=published_hint)


def metadata_client():
    """The model client for reading metadata, or None when no model is configured."""

    from law_agent.config import require_llm_config
    from law_agent.llm.openai_compatible import OpenAICompatibleClient

    try:
        return OpenAICompatibleClient(require_llm_config())
    except RuntimeError:
        logger.warning("未配置模型，新来源的元数据将按规则推断并交人工审核")
        return None


def read_source_metadata(
    *, client, source_id: str, title: str, text: str, url: str, source_site: str, file_format: str,
    raw_text: str | None = None, published_hint: str | None = None, today: date | None = None,
) -> SourceInference:
    """The model's reading of a new source; the rules' when the model is not configured or fails.

    A rules draft is only a prefill: it is marked as such and never publishes without a reviewer.
    """

    common = dict(source_id=source_id, title=title, text=text, url=url, source_site=source_site,
                  file_format=file_format, published_hint=published_hint, today=today)
    reason = "未配置模型"
    if client is not None:
        try:
            return read_with_model(client=client, **common).inference
        except Exception as exc:  # noqa: BLE001 - any model failure falls back to the rules for review
            logger.warning("模型读取元数据失败，改用规则推断：%s", exc)
            reason = f"模型读取失败（{type(exc).__name__}）"
    inference = infer_source(raw_text=raw_text, **common)
    return replace(inference, evidence={**inference.evidence, "reader": f"{reason}，以下为规则推断，需人工确认"})
