"""Conservative evidence validation and arithmetic for the bounded M2 slice.

Not general clinical NLP. Exact quotes + role guards reject known bad bindings,
but do not establish full semantic entailment. Unhandled language requires review.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from .contracts import CaseRecord, Rule, Taxonomy, aware_time
from .data import applicable, rule_conflicts
from .m2_contracts import PKExtraction

TZ = ZoneInfo("Asia/Shanghai")
DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
CLOCK = re.compile(r"(?<![\d:])(?:[01]\d|2[0-3]):[0-5]\d(?![\d:])")


class EvidenceError(ValueError):
    pass


def locate(text: str, quote: str, field: str, *, evidence_id: str) -> dict:
    if not quote or text.count(quote) != 1:
        raise EvidenceError(f"{field}: evidence must occur exactly once in the input.")
    start = text.index(quote)
    return {"evidence_id": evidence_id, "origin": "record", "field": field,
            "quote": quote, "start_char": start, "end_char": start + len(quote)}


def local_clause(text: str, quote: str) -> str:
    """Inspect surrounding context, not just an LLM-selected positive substring."""
    start = text.index(quote)
    end = start + len(quote)
    before = list(re.finditer(r"[，。；;\n]", text[:start]))
    after = re.search(r"[，。；;\n]", text[end:])
    return text[before[-1].end() if before else 0:end + after.start() if after else len(text)]


def validate_extraction(record: CaseRecord, extraction: PKExtraction) -> dict:
    evidence = []
    for field in ("scope_quote", "same_sample_quote", "date_quote", "timezone_quote",
                  "collected_time_quote", "centrifuged_time_quote"):
        quote = getattr(extraction, field)
        if quote is not None:
            evidence.append(locate(record.text, quote, field, evidence_id=f"F{len(evidence)+1}"))
    for field in ("other_issue_quotes", "uncertainty_quotes"):
        for quote in getattr(extraction, field):
            evidence.append(locate(record.text, quote, field, evidence_id=f"F{len(evidence)+1}"))

    def unresolved(status, reason, missing=None):
        return {"status": status, "reason": reason, "missing_facts": missing or [],
                "evidence": evidence, "facts": None}

    # Explicit engineering guard, not a second classifier. Do not silently
    # reduce multiple issues to an apparently clean PK-only result.
    if extraction.scope == "unsupported_or_multiple" or extraction.other_issue_quotes:
        return unresolved("unsupported_scope", "M2只支持单份PK样本；其他问题/多问题未自动处理，整条转人工。")
    if re.search(r"AE|不良事件|恶心|授权|EDC|库存|试验药|试验用药|访视|另一份|两份样本", record.text, re.I):
        return unresolved("unsupported_scope", "检测到超出单份PK检查范围的文字，转人工；不代表这些问题已分类。")
    if extraction.uncertainty_quotes or re.search(r"未确定|不确定|矛盾|冲突|计划|预计|次日|次天|第二天|昨天|昨日|分别|有误|更正|作废|不同受试者|不同样本", record.text):
        return unresolved("ambiguous_evidence", "有不确定、计划或跨日/多来源线索，M2不选择一个时间作定论。")
    if extraction.scope != "single_pk":
        return unresolved("unsupported_scope", "模型未能确认本记录属于M2支持的单份PK样本检查。")
    if re.search(r"忽略|跳过|直接通过|无须复核|无需复核|系统提示|system\s*prompt", record.text, re.I):
        return unresolved("unsupported_scope", "原文含试图控制分析流程的指令，M2保守转人工，不执行这些指令。")
    missing = [field for field in ("scope_quote", "same_sample_quote", "date_quote", "timezone_quote",
                                   "collected_time_quote", "centrifuged_time_quote")
               if getattr(extraction, field) is None]
    if missing:
        return unresolved("needs_information", "缺少可核实的样本、日期、时区或处理时间，不能用提交时间补齐。", missing)
    # Bound field evidence so an entire-record quote cannot mark arbitrary extra
    # sentences as checked. These are deliberately narrow demonstration limits.
    limits = {"same_sample_quote": 32, "date_quote": 20, "timezone_quote": 24,
              "collected_time_quote": 48, "centrifuged_time_quote": 48}
    for field, limit in limits.items():
        quote = getattr(extraction, field)
        if len(quote) > limit or re.search(r"[，。；;\n]", quote):
            raise EvidenceError(f"{field}: require a short, single-clause evidence span.")
    if not re.fullmatch(r"(?:SYN-SUBJ-[A-Za-z0-9_-]+)?同一(?:份)?\s*PK\s*样本", extraction.same_sample_quote, re.I):
        return unresolved("needs_information", "M2需要原文明确确认同一份PK样本。", ["same_sample"])
    if re.search(r"不|非|未|无|可能|假设", local_clause(record.text, extraction.same_sample_quote)):
        return unresolved("ambiguous_evidence", "同一样本描述的上下文含否定或不确定含义，不能截取正面子串。")
    if not re.search(r"PK\s*样本", extraction.scope_quote, re.I):
        raise EvidenceError("Scope evidence does not identify a PK sample.")
    if not re.fullmatch(r"(?:均为|时间均为|所有时间均为)?北京时间", extraction.timezone_quote):
        return unresolved("needs_information", "M2仅支持原文明示北京时间。", ["timezone"])
    if re.search(r"不|非|未|无|可能|假设|待确认", local_clause(record.text, extraction.timezone_quote)):
        return unresolved("ambiguous_evidence", "时区上下文存在否定或不确定，不能默认北京时间。")
    # A quote must bind a clock to the intended event, not just contain a clock.
    clocks = []
    for quote, field, role in ((extraction.collected_time_quote, "collected_at", "采血"),
                              (extraction.centrifuged_time_quote, "centrifuged_at", "开始离心")):
        context = local_clause(record.text, quote)
        if role not in quote or re.search(r"提交|计划|预计|未|无|没有|不|非|结束|完成|更正|作废", context):
            raise EvidenceError(f"{field}: missing/negated/wrong event role in evidence.")
        found = CLOCK.findall(quote)
        if len(found) != 1:
            raise EvidenceError(f"{field}: expected exactly one HH:MM clock in role evidence.")
        if not re.fullmatch(re.escape(role) + r"(?:时间)?(?:为|于|是)?(?:北京时间)?\s*" + re.escape(found[0]), quote):
            raise EvidenceError(f"{field}: event/clock binding is outside M2's supported explicit wording.")
        clocks.append(found[0])
    dates = DATE.findall(extraction.date_quote)
    all_dates = set(DATE.findall(record.text))
    if re.search(r"不是|并非|未确定|可能|假设|待确认", local_clause(record.text, extraction.date_quote)):
        return unresolved("ambiguous_evidence", "日期上下文存在否定或不确定，不能直接采用。")
    if len(dates) != 1 or extraction.date_quote != dates[0] or all_dates != {dates[0]}:
        return unresolved("ambiguous_evidence", "M2只支持原文中明确、唯一的同日日期，不能猜测跨日关系。")
    # Additional unmatched times are not ignored, even if the LLM omitted them.
    if sorted(CLOCK.findall(record.text)) != sorted(clocks):
        return unresolved("ambiguous_evidence", "原文还有其他或重复时刻，不能确认唯一的采血/离心时间对。")
    try:
        collected = datetime.fromisoformat(f"{dates[0]}T{clocks[0]}:00").replace(tzinfo=TZ)
        centrifuged = datetime.fromisoformat(f"{dates[0]}T{clocks[1]}:00").replace(tzinfo=TZ)
    except ValueError as exc:
        raise EvidenceError("Invalid calendar date.") from exc
    if aware_time(record.event_at).astimezone(TZ).date() != collected.date():
        return unresolved("ambiguous_evidence", "输入上下文日期与原文事件日期不一致，不能选择规则版本。")
    if centrifuged < collected:
        return unresolved("ambiguous_evidence", "离心早于采血，不能自行推断为次日。")
    # Inspect clauses omitted by the extraction, not only the selected facts.
    # The scope quote is intentionally excluded (a model may quote the whole
    # record). Unknown extra clauses route to review, not general NLP success.
    fact_spans = [e for e in evidence if e["field"] in limits]
    allowed_requests = {
        "请检查样本处理是否符合本研究方案", "本次仅检查采血至开始离心的时间间隔",
        "请按当前研究已发布且适用的规则检查样本处理时间", "请核查时间间隔",
    }
    for clause in re.finditer(r"[^，。；;\n]+", record.text):
        if clause.group().strip() in allowed_requests:
            continue
        masked = list(clause.group())
        for e in fact_spans:
            if clause.start() <= e["start_char"] and e["end_char"] <= clause.end():
                for index in range(e["start_char"] - clause.start(), e["end_char"] - clause.start()):
                    masked[index] = ""
        remaining = "".join(masked).strip()
        # Only punctuation glue, fictional subject ID, and known prefixes may
        # remain. A fact somewhere in a clause does not validate the rest of it.
        if not re.fullmatch(r"(?:【合成虚拟记录】)?(?:SYN-SUBJ-[A-Za-z0-9_-]+)?(?:的)?|(?:均为|时间均为|所有时间均为)", remaining):
            return unresolved("unsupported_scope", "存在抽取未覆盖的额外文字或未支持表达；M2不能检查PK部分后忽略其余内容。")
    return {"status": "ready", "missing_facts": [], "evidence": evidence,
            "facts": {"collected_at": collected.isoformat(), "centrifuged_at": centrifuged.isoformat(),
                      "same_sample": True, "timezone": "Asia/Shanghai",
                      "event_time_basis": "从原文日期及开始离心时间规范化；不是记录提交时间"}}


def check_pk(record: CaseRecord, validated: dict, candidate_rules: list[Rule],
             all_rules: list[Rule], taxonomy: Taxonomy) -> dict:
    """No model, reference answers, or threshold supplied by an LLM."""
    if validated["status"] != "ready":
        return {**validated, "findings": [], "review_required": True, "triage_priority": "priority",
                "risk": "待定", "calculation": None, "rule_keys": []}
    facts = validated["facts"]
    end_record = record.model_copy(update={"event_at": facts["centrifuged_at"]})
    start_record = record.model_copy(update={"event_at": facts["collected_at"]})
    scoped = [r for r in all_rules if applicable(r, end_record)]
    by_key = {r.key: r for r in all_rules}
    if len(by_key) != len(all_rules):
        raise EvidenceError("Duplicate canonical rule version keys.")
    for candidate in candidate_rules:
        if candidate.key not in by_key or candidate != by_key[candidate.key]:
            raise EvidenceError("Candidate rule differs from the canonical approved rule version.")
    conflicts = rule_conflicts(scoped)
    base = {"evidence": validated["evidence"], "facts": facts, "findings": [],
            "review_required": True, "triage_priority": "priority", "risk": "待定",
            "missing_facts": [], "calculation": None, "rule_keys": []}
    if conflicts:
        return {**base, "status": "rule_conflict", "conflicts": conflicts,
                "reason": "同一适用范围的规则冲突；不让检索排序或模型任选其一。"}
    expected_pk = [r for r in scoped if r.check_type == "elapsed_minimum"]
    selected = [by_key[r.key] for r in candidate_rules if r.check_type == "elapsed_minimum" and applicable(r, end_record)]
    if not selected or {r.key for r in selected} != {r.key for r in expected_pk}:
        return {**base, "status": "rule_not_found", "reason": "检索未完整找到适用PK规则，不能视为没有问题。"}
    if any(not applicable(r, start_record) for r in selected):
        return {**base, "status": "ambiguous_evidence", "reason": "采血和离心跨越规则有效区间，转人工确认适用时点。"}
    if len(selected) != 1:
        return {**base, "status": "rule_conflict", "reason": "M2需要唯一适用的PK规则，不自动合并多个规则。"}
    rule = selected[0]
    path = next((p for p in taxonomy.paths if p.l3_id == rule.output_l3_id), None)
    if path is None:
        raise EvidenceError("Rule classification is outside the taxonomy allow-list.")
    start, end = (aware_time(facts[k]).astimezone(timezone.utc) for k in ("collected_at", "centrifuged_at"))
    delta = end - start
    microseconds = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    minutes = Decimal(microseconds) / Decimal(60_000_000)
    threshold = rule.parameters["minimum_minutes"]
    if type(threshold) is not int or threshold <= 0:
        raise EvidenceError("Invalid rule threshold.")
    hit = minutes < threshold
    finding = {"l3_id": path.l3_id, "l1": path.l1, "l2": path.l2, "l3": path.l3,
               "risk": rule.risk, "suggested_decision": rule.suggested_decision,
               "suggested_action": rule.suggested_action, "requires_human_review": True,
               "l4_candidates": [c.model_dump() for c in path.l4_candidates], "rule_keys": [rule.key]}
    evidence = base["evidence"] + [{"evidence_id": "R1", "origin": "rule",
                                    "rule_key": rule.key, **rule.source.model_dump()}]
    relation = "<" if hit else ">="
    return {**base, "status": "proposed_findings" if hit else "no_finding_for_checked_rule",
            "triage_priority": "priority" if hit and rule.risk == "高" else "routine",
            "risk": rule.risk if hit else "不适用（本项未命中）", "findings": [finding] if hit else [],
            "rule_keys": [rule.key], "evidence": evidence,
            "calculation": {"operation": "elapsed_minutes", "start": facts["collected_at"],
                            "end": facts["centrifuged_at"], "actual_minutes": str(minutes),
                            "minimum_minutes": threshold, "relation": relation, "hit": hit},
            "reason": f"原文时间间隔为{minutes}分钟，{minutes} {relation} {threshold}；"
                      + ("本条虚拟规则命中，建议待人工复核。" if hit else "本条时间不足规则未命中，不代表其他检查通过。")}
