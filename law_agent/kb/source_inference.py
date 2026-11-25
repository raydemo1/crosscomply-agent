"""Infer a new legal source's governed metadata from its own text, with the evidence for each field.

Every route that brings a new source in (the discovery worker, the CLI, the web
import) used to default an unknown file to "guideline / authority unknown /
interpretation auxiliary" with no dates: a departmental rule came in
uncitable and undated unless someone filled every field by hand. This reads
what the document says about itself — its issuing order ("国家互联网信息办公室令
第16号"), its title ("……条例", "GB/T", "答记者问", "……负面清单"), its entry
into force ("自2024年3月22日起施行") — and returns a draft record, the
sentence each field was read from, and the fields it could not settle, so a
reviewer confirms the prefilled draft rather than writing it.

These rules are the fallback: the model reads new sources (:mod:`law_agent.kb.source_llm`), and
the rules prefill a draft only when no model is configured or the call fails. Such a draft always
waits for a reviewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from law_agent.data.schemas import SourceRecord

# Agencies whose orders are departmental rules, in the form the order line names them.
AGENCIES = (
    "国家互联网信息办公室", "国家发展和改革委员会", "工业和信息化部", "公安部", "交通运输部", "国家安全部",
    "国家市场监督管理总局", "自然资源部", "司法部", "商务部", "财政部", "中国人民银行", "国家金融监督管理总局",
    "中国证券监督管理委员会", "国家卫生健康委员会", "国家数据局", "国家保密局", "教育部", "科学技术部",
    "人力资源和社会保障部", "生态环境部", "住房和城乡建设部", "农业农村部", "文化和旅游部", "海关总署",
    "国家税务总局", "国家广播电视总局", "国家药品监督管理局", "中国民用航空局", "国家邮政局",
)
# Short names agencies sign with, mapped to the full name.
AGENCY_ALIASES = {
    "国家网信办": "国家互联网信息办公室", "中国证监会": "中国证券监督管理委员会", "国家发展改革委": "国家发展和改革委员会",
    "工业和信息化部": "工业和信息化部", "市场监管总局": "国家市场监督管理总局", "金融监管总局": "国家金融监督管理总局",
    "国家卫生健康委": "国家卫生健康委员会", "国家档案局": "国家档案局", "人民银行": "中国人民银行",
}


# The organ a non-normative page (Q&A, guide, template) is published by, when the text does not sign it.
SITE_ISSUERS = {
    "cac.gov.cn": "国家互联网信息办公室", "csrc.gov.cn": "中国证券监督管理委员会", "miit.gov.cn": "工业和信息化部",
    "samr.gov.cn": "国家市场监督管理总局", "pbc.gov.cn": "中国人民银行", "nfra.gov.cn": "国家金融监督管理总局",
    "mnr.gov.cn": "自然资源部", "nhc.gov.cn": "国家卫生健康委员会", "mps.gov.cn": "公安部", "mot.gov.cn": "交通运输部",
    "nda.gov.cn": "国家数据局", "court.gov.cn": "最高人民法院", "spp.gov.cn": "最高人民检察院",
    "tc260.org.cn": "全国网络安全标准化技术委员会秘书处",
}
# A provincial or municipal organ: "北京市互联网信息办公室", "广西壮族自治区商务厅", "上海市数据局".
_LOCAL_ORGAN_RE = re.compile(
    r"(?:北京|天津|上海|重庆)市[\u4e00-\u9fff]{1,14}?(?:办公室|委员会|局|厅)"
    r"|[\u4e00-\u9fff]{2,8}?(?:省|自治区)[\u4e00-\u9fff]{1,14}?(?:办公室|委员会|局|厅)"
    r"|[\u4e00-\u9fff]{2,4}市[\u4e00-\u9fff]{1,14}?(?:办公室|委员会|局|厅)"
)


def site_issuer(url: str) -> str | None:
    host = urlsplit(url).hostname or ""
    return next((organ for site, organ in SITE_ISSUERS.items() if host == site or host.endswith("." + site)), None)


def local_organs(text: str) -> list[str]:
    """Provincial and municipal organs named in ``text``, in order, each once."""

    return list(dict.fromkeys(match.group(0) for match in _LOCAL_ORGAN_RE.finditer(text)))


def joint_issuer(organs: list[str]) -> str | None:
    return organs[0] if len(organs) == 1 else (f"{organs[0]}等{len(organs)}部门" if organs else None)


def agencies_in(text: str) -> list[str]:
    """Agencies named in ``text``, in the order the text names them (the lead agency first)."""

    found: dict[str, int] = {}
    for name in (*AGENCIES, *AGENCY_ALIASES):
        position = text.find(name)
        if position >= 0:
            full = AGENCY_ALIASES.get(name, name)
            found[full] = min(found.get(full, position), position)
    return sorted(found, key=found.__getitem__)


REGIONS = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "四川", "贵州", "云南", "陕西", "甘肃",
    "青海", "宁夏", "新疆", "西藏", "内蒙古", "深圳", "厦门", "粤港澳大湾区",
)
REGION_CODES = {
    "北京": "CN-BJ", "天津": "CN-TJ", "上海": "CN-SH", "重庆": "CN-CQ", "河北": "CN-HE", "山西": "CN-SX",
    "辽宁": "CN-LN", "吉林": "CN-JL", "黑龙江": "CN-HL", "江苏": "CN-JS", "浙江": "CN-ZJ", "安徽": "CN-AH",
    "福建": "CN-FJ", "江西": "CN-JX", "山东": "CN-SD", "河南": "CN-HA", "湖北": "CN-HB", "湖南": "CN-HN",
    "广东": "CN-GD", "广西": "CN-GX", "海南": "CN-HI", "四川": "CN-SC", "贵州": "CN-GZ", "云南": "CN-YN",
    "陕西": "CN-SN", "甘肃": "CN-GS", "青海": "CN-QH", "宁夏": "CN-NX", "新疆": "CN-XJ", "西藏": "CN-XZ",
    "内蒙古": "CN-NM", "深圳": "CN-GD-SZ", "厦门": "CN-FJ-XM",
}
INDUSTRIES = ("汽车", "车联网", "金融", "银行", "证券", "保险", "工业和信息化", "医疗", "医药", "卫生健康", "测绘",
              "地理信息", "电信", "交通运输", "民航", "教育")
_NUMERAL = "一二三四五六七八九十百千零〇0123456789"
# "令第16号", "令（第9号）", "令〔2025〕第3号", "令【第218号】", "第24号令".
ORDER_RE = re.compile(
    rf"令\s*(?:〔\s*\d{{4}}\s*〕)?\s*[（(【\[]?\s*第\s*[{_NUMERAL}]+\s*号|第[{_NUMERAL}]+号令")
DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
ISO_DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
ENTRY_INTO_FORCE_RE = re.compile(r"自\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起\s*(?:施行|实施|生效)")
FROM_PUBLICATION_RE = re.compile(r"自\s*(?:公布|发布|印发)之日起\s*(?:施行|实施|生效)")
DATELINE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s+\d{1,2}:\d{2}\s+来源")
STANDARD_DATES_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})\s*发布.{0,40}?(20\d{2}-\d{2}-\d{2})\s*实施", re.DOTALL)
# Version markers: the same instrument across editions ("（第三版）", "（2025版）", "（试行）", "（2024修订）").
VERSION_MARK_RE = re.compile(
    r"[（(]\s*(?:第[一二三四五六七八九十\d]+版|20\d{2}\s*年?\s*版|20\d{2}\s*年?\s*修(?:订|正)|修订版|试行|征求意见稿)\s*[）)]"
)
# Page titles carry the site after an underscore: "促进和规范数据跨境流动规定_国家互联网信息办公室_中国政府网".
_SITE_SUFFIX_RE = re.compile(r"\s*[_｜|]\s*.*$")
# A publication date in the page address: cac.gov.cn/2024-03/22/…, gov.cn/…/2023-02/25/content_…, npc.gov.cn/…/t20190521_…
_URL_DATE_RES = (re.compile(r"/(20\d{2})-(\d{2})/(\d{2})/"), re.compile(r"/t(20\d{2})(\d{2})(\d{2})_"),
                 re.compile(r"/(20\d{2})(\d{2})(\d{2})/"))
AMENDED_RE = re.compile(r"根据\s*(20\d{2})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日.{0,80}?(?:修正|修订|修改)")


@dataclass(frozen=True)
class SourceInference:
    source: SourceRecord
    evidence: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    reader: str = "rules"  # "model", or "rules" when the model was unavailable
    # "document": the page carries the material itself; "issuing_notice": it only issues attached files.
    page_kind: str = "document"
    follow: tuple[int, ...] = ()  # 1-based positions of the attachments to index instead of the notice
    # Optional verbatim anchors (web pages only) for deterministic body trimming when page chrome leaked in.
    body_start_quote: str | None = None
    body_end_quote: str | None = None

    @property
    def settled(self) -> bool:
        """The model read every field with confidence: nothing for a reviewer to decide.

        A rules draft is never settled: the rules are only the fallback prefill."""
        return self.reader == "model" and not self.unresolved


# A bulletin title can lead with the promulgating order or a notice number:
# "中华人民共和国国务院令（第469号）　　中华人民共和国测绘成果管理条例", "【第44号公告】《……规定》".
_TITLE_PREFIX_RE = re.compile(r"^(?:[\u4e00-\u9fff]{0,30}令\s*[（(]\s*第\s*\d+\s*号\s*[）)]|【[^】]{1,20}】)\s*")
# "中国人民银行令〔2025〕第3号（中国人民银行业务领域数据安全管理办法）": the order, then the instrument.
_ORDER_TITLE_RE = re.compile(r"^[\u4e00-\u9fff]{0,30}令\s*[〔(（]?\s*\d{0,4}\s*[〕)）]?\s*第\s*\d+\s*号\s*[（(](.+)[）)]$")


def clean_title(title: str) -> str:
    title = _SITE_SUFFIX_RE.sub("", re.sub(r"[\s\u3000]+", " ", title.strip()))
    if match := _ORDER_TITLE_RE.match(title):
        title = match.group(1)
    title = _TITLE_PREFIX_RE.sub("", title)
    title = re.sub(r"\s*[（(]\s*全文\s*[）)]\s*$|全文$", "", title).strip()
    # A title quoted as a whole is the instrument's name without the quotation marks.
    return re.sub(r"^《([^《》]+)》$", r"\1", title)


def instrument_key(title: str) -> str:
    """The instrument a title names, without its edition: editions of one instrument share it."""

    return re.sub(r"\s+", "", VERSION_MARK_RE.sub("", clean_title(title)))


_CN_DIGITS = {"〇": 0, "○": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_DATE_RE = re.compile(r"([〇○零一二三四五六七八九]{4})\s*年\s*([一二三四五六七八九十]{1,3})\s*月\s*([一二三四五六七八九十]{1,3})\s*日")


def _cn_number(value: str) -> int:
    if "十" not in value:
        return int("".join(str(_CN_DIGITS[char]) for char in value))
    tens, _, ones = value.partition("十")
    return (_CN_DIGITS[tens] if tens else 1) * 10 + (_CN_DIGITS[ones] if ones else 0)


def arabic_dates(text: str) -> str:
    """Write a date set in Chinese numerals ("二○○六年五月二十七日") with digits, as orders sign them."""

    return _CN_DATE_RE.sub(lambda m: f"{_cn_number(m.group(1))}年{_cn_number(m.group(2))}月{_cn_number(m.group(3))}日",
                           text)


def _iso(match: re.Match[str]) -> str | None:
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def _compact(text: str) -> str:
    return re.sub(r"[\s*#]+", "", text)


def _snippet(text: str, start: int, end: int) -> str:
    return text[max(0, start - 20): end + 20].replace("\n", " ").strip()


def _first(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    return pattern.search(text)


def _region(title: str) -> str | None:
    return next((region for region in REGIONS if region in title), None)


def region_code(region: str | None, title: str) -> str:
    if region == "粤港澳大湾区":
        return "CN-GBA-MO" if "澳门" in title else "CN-GBA-HK" if "香港" in title else "CN"
    return REGION_CODES.get(region or "", "CN")


def _industry(title: str) -> str | None:
    return next((industry for industry in INDUSTRIES if industry in title), None)


# The fallback reading of a page that only issues attached files: short, no articles of its own, and
# linking a file or saying it issues one ("现将《……》印发给你们").
NOTICE_SHELL_MAX_CHARS = 2000
_ARTICLE_LINE_RE = re.compile(r"(?m)^\s*第[一二三四五六七八九十百零〇\d]+条")
_ISSUING_RE = re.compile(r"印发给你们|印发给你单位|现予印发|现印发|随文印发")


def notice_shell(text: str, attachments: list[tuple[str, str]]) -> bool:
    return (len(text) <= NOTICE_SHELL_MAX_CHARS and not _ARTICLE_LINE_RE.search(text)
            and (bool(attachments) or bool(_ISSUING_RE.search(text))))


def infer_source(
    *, source_id: str, title: str, text: str, url: str, source_site: str, file_format: str,
    raw_text: str | None = None, published_hint: str | None = None, today: date | None = None,
    attachments: list[tuple[str, str]] | None = None,
) -> SourceInference:
    """Draft a ``SourceRecord`` for a legal source.

    ``text`` is the cleaned body; ``raw_text`` the parse before cleaning, which still has the page's
    dateline; ``published_hint`` a date the search engine reported for the page.
    """

    today = today or date.today()
    page_title, title = title, clean_title(title)
    text = arabic_dates(text)
    raw_text = arabic_dates(raw_text) if raw_text else None
    head = text[:3000]
    compact_head = _compact(head)
    preamble = _compact(re.split(r"第一条", head, maxsplit=1)[0])[:800]
    evidence: dict[str, str] = {}
    unresolved: list[str] = []
    # An enacting order heads the instrument; one named further down is only a citation of another rule.
    # The page title may carry the order the body leaves out: "中国人民银行令〔2025〕第3号（……办法）".
    preamble = _compact(page_title) + "\n" + preamble
    order = ORDER_RE.search(preamble) if not re.search(r"公告|通知|决定|意见", title) else None
    if order and re.search(r"(?:主席|国务院)令", preamble[max(0, order.start() - 4): order.end()]):
        order = None  # a law's or an administrative regulation's order: its title settles it below
    region, industry = _region(title), _industry(title)

    def classify() -> tuple[str, str, str, str | None]:
        """doc_type, authority, citation_role, issuing_body."""
        if re.search(r"GB/?T\s*\d|GB\s*\d{4,}", title + head[:60]):
            evidence["doc_type"] = "标题或封面为国家标准编号（GB/T）"
            return "standard", "national_standard", "implementation_reference", "国家市场监督管理总局、国家标准化管理委员会"
        if re.search(r"答记者问|问答|答问|解读", title):
            evidence["doc_type"] = f"标题含“{re.search(r'答记者问|问答|答问|解读', title).group(0)}”"
            return "faq", "public_interpretation", "interpretation_auxiliary", joint_issuer(
                local_organs(compact_head[:600]) or agencies_in(compact_head[:600])[:1])
        if "网络安全标准实践指南" in title + head[:300]:
            evidence["doc_type"] = "标题为《网络安全标准实践指南》"
            return "guideline", "technical_guidance", "implementation_reference", "全国网络安全标准化技术委员会秘书处"
        if (match := re.search(r"法释〔\d{4}〕\d+号", compact_head)):
            evidence["doc_type"] = f"正文含司法解释文号“{match.group(0)}”"
            body = "最高人民检察院" if "最高人民检察院" in compact_head[:600] else "最高人民法院"
            return "judicial_interpretation", "judicial_interpretation", "primary_legal_basis", body
        npc_page = re.search(r"(?:^|\.)npc\.gov\.cn$", urlsplit(url).hostname or "")
        if re.fullmatch(r"中华人民共和国.+法", title) and (npc_page or re.search(r"主席令|全国人民代表大会", compact_head[:1500])):
            evidence["doc_type"] = ("标题为“中华人民共和国……法”，来自全国人大网站" if npc_page
                                    else "标题为“中华人民共和国……法”，正文含全国人大或主席令")
            return "law", "national_law", "primary_legal_basis", "全国人民代表大会常务委员会"
        if title.endswith("条例") and not region:
            evidence["doc_type"] = ("标题为全国性“……条例”，正文含“国务院令”" if "国务院令" in compact_head
                                    else "标题为全国性“……条例”（行政法规的名称形式）")
            return "regulation", "administrative_regulation", "primary_legal_basis", "国务院"
        if title.endswith("条例") and region and "人民代表大会常务委员会" in compact_head:
            evidence["doc_type"] = f"标题为{region}“……条例”，正文含人大常委会公告"
            evidence["citation_role"] = f"地方性法规，适用于{region}"
            return "regulation", "local_regulation", "conditional_local_basis", f"{region}市人民代表大会常务委员会"
        if order:
            agencies = agencies_in(preamble[: order.end() + 10])
            evidence["doc_type"] = f"含部门规章令号“{_snippet(preamble, order.start(), order.end())}”"
            body = agencies[0] if len(agencies) == 1 else (f"{agencies[0]}等{len(agencies)}部门" if agencies else None)
            if industry:
                evidence["citation_role"] = f"标题含行业“{industry}”，为行业性依据"
                return "regulation", "departmental_rule", "conditional_industry_basis", body
            return "regulation", "departmental_rule", "primary_legal_basis", body
        if "清单" in title and region:
            # A list a region issues under a rule's authority (流动规定第六条) binds those within the zone.
            evidence["doc_type"] = f"标题为{region}管理清单，属地方规范性文件"
            evidence["citation_role"] = f"适用于{region}，为地方性条件依据"
            return "policy", "local_normative_document", "conditional_local_basis", joint_issuer(
                local_organs(compact_head[:1500] + _compact(text[-800:])))
        if re.search(r"范本|标准合同$", title):
            evidence["doc_type"] = "标题为合同范本或标准合同文本"
            return "contract", "ministry_policy", "implementation_reference", joint_issuer(
                agencies_in(compact_head[:800]))
        kinds = (("guideline", r"指南|指引|规范|规则|细则"), ("policy", r"通知|公告|意见|决定|办法|规定"))
        for doc_type, pattern in kinds:
            if not (match := re.search(pattern, title)):
                continue
            evidence["doc_type"] = f"标题含“{match.group(0)}”，正文开头无部门规章令号"
            agencies = (agencies_in(title + compact_head[:800])
                        or (local_organs(compact_head[:800]) if region else [])
                        or [organ for organ in [site_issuer(url)] if organ and not region])
            authority = ("local_normative_document" if region and doc_type == "policy"
                         else "ministry_policy" if agencies or doc_type == "policy" else "public_interpretation")
            body = joint_issuer(agencies)
            if region:
                evidence["citation_role"] = f"标题含地区“{region}”，为地方性条件依据"
                return doc_type, authority, "conditional_local_basis", body
            if industry:
                evidence["citation_role"] = f"标题含行业“{industry}”，为行业性条件依据"
                return doc_type, authority, "conditional_industry_basis", body
            return doc_type, authority, "implementation_reference", body
        unresolved.append("doc_type")
        return "guideline", "unknown", "interpretation_auxiliary", None

    doc_type, authority, citation_role, issuing_body = classify()
    if issuing_body:
        evidence.setdefault("issuing_body", "由文号或正文开头的机关名称得出")
    elif doc_type not in {"law", "regulation", "judicial_interpretation"} and (issuing_body := site_issuer(url)):
        evidence["issuing_body"] = f"发布网站为{issuing_body}官网（正文未署发布机关）"
    if region and citation_role != "conditional_local_basis" and "清单" not in title:
        evidence.setdefault("applicable_region", f"标题含地区“{region}”")

    # Dates: a standard's "发布 / 实施" line, the page's dateline, the order's signature date, the address.
    publish_date: str | None = None
    effective_date: str | None = None
    raw_head = (raw_text or text)[:3000]
    if doc_type == "standard" and (match := STANDARD_DATES_RE.search(head)):
        publish_date, effective_date = match.group(1), match.group(2)
        evidence["publish_date"] = evidence["effective_date"] = _snippet(head, match.start(), match.end())
    if publish_date is None and (match := DATELINE_RE.search(raw_head)):
        publish_date = _iso(match)
        evidence["publish_date"] = f"页面发布时间“{_snippet(raw_head, match.start(), match.end())}”"
    if publish_date is None and doc_type in {"regulation", "law", "judicial_interpretation", "policy"}:
        preamble_text = re.split(r"第一条", head, maxsplit=1)[0]
        in_force = {m.start() for m in ENTRY_INTO_FORCE_RE.finditer(preamble_text)}
        dates = [d for m in DATE_RE.finditer(preamble_text)
                 if not any(start <= m.start() <= start + 4 for start in in_force) and (d := _iso(m))]
        if dates:
            publish_date = max(dates)
            evidence["publish_date"] = "发布令或公告中最晚的日期（签署日期）"
    if publish_date is None:
        for pattern in _URL_DATE_RES:
            if (match := pattern.search(url)) and (found := _iso(match)):
                publish_date = found
                evidence["publish_date"] = f"官方页面地址中的日期（{match.group(0).strip('/_')}）"
                break
    if publish_date is None and published_hint:
        publish_date = published_hint[:10]
        evidence["publish_date"] = "检索结果给出的页面发布日期"
    amended = AMENDED_RE.search(head[:600]) if doc_type in {"law", "regulation"} else None
    if doc_type != "faq" and not amended:
        if effective_date is None and (match := ENTRY_INTO_FORCE_RE.search(text)):
            effective_date = _iso(match)
            evidence["effective_date"] = f"正文“{_snippet(text, match.start(), match.end())}”"
        if effective_date is None and (match := FROM_PUBLICATION_RE.search(text)) and publish_date:
            effective_date = publish_date
            evidence["effective_date"] = f"正文“{match.group(0)}”，取发布日期"
    if amended:
        evidence["effective_date"] = (f"正文为修正后文本（{_snippet(head, amended.start(), amended.end())}），"
                                      "现行文本的施行日期以修改决定为准，需核对")

    is_norm = citation_role in {"primary_legal_basis", "conditional_local_basis", "conditional_industry_basis"} and (
        doc_type in {"law", "regulation", "judicial_interpretation"})
    if is_norm and not effective_date:
        unresolved.append("effective_date")
    if is_norm and not issuing_body:
        unresolved.append("issuing_body")

    law_status = "unknown"
    if effective_date:
        law_status = "effective" if date.fromisoformat(effective_date) <= today else "not_yet_effective"
    elif doc_type in {"faq", "guideline", "standard", "contract", "policy"} and publish_date:
        law_status = "effective"

    source = SourceRecord(
        source_id=source_id, library_kind="legal", title=title, source_url=url, source_site=source_site,
        doc_type=doc_type, authority=authority, citation_role=citation_role, law_status=law_status,
        publish_date=publish_date, effective_date=effective_date, issuing_body=issuing_body,
        applicable_region=region_code(region, title), instrument_key=instrument_key(title),
        file_format=file_format, include_in_mvp=True,
    )
    attachments = attachments or []
    if notice_shell(text, attachments):
        evidence["page_kind"] = "页面短、没有自己的条文，且带附件或写明印发：按印发通知处理"
        return SourceInference(source=source, evidence=evidence, unresolved=unresolved, page_kind="issuing_notice",
                               follow=tuple(range(1, len(attachments) + 1)))
    return SourceInference(source=source, evidence=evidence, unresolved=unresolved)


def version_date(source: SourceRecord) -> str | None:
    return source.effective_date or source.publish_date


def previous_editions(new: SourceRecord, existing: list[SourceRecord]) -> list[SourceRecord]:
    """Published editions of the instrument ``new`` is an edition of (same instrument, another source)."""

    key = new.instrument_key or instrument_key(new.title)
    return [
        item for item in existing
        if item.source_id != new.source_id and item.library_kind == new.library_kind
        and (item.instrument_key or instrument_key(item.title)) == key
    ]


def supersession_problem(new: SourceRecord, editions: list[SourceRecord]) -> str | None:
    """Why ``new`` cannot simply take over from ``editions``, or None when it can."""

    new_date = version_date(new)
    for item in editions:
        old_date = version_date(item)
        if not new_date or not old_date:
            return f"新版与旧版《{item.title}》缺少可比较的生效或发布日期，请核实版本关系"
        if new_date <= old_date:
            return f"新版日期 {new_date} 未晚于已入库的《{item.title}》（{old_date}），请核实哪一版现行"
    return None


def retire_editions(new: SourceRecord, editions: list[SourceRecord]) -> list[SourceRecord]:
    """The old editions, now ending where ``new`` takes over and sharing its instrument key."""

    key = new.instrument_key or instrument_key(new.title)
    end = version_date(new)
    return [item.model_copy(update={"instrument_key": key,
                                    "valid_to": min(filter(None, [item.valid_to, end]), default=None)})
            for item in editions]
