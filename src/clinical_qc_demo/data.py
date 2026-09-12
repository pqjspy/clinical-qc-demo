"""Separate readers for input, knowledge and authored reference data.

Input/knowledge readers never read reference answers. M1 checks authored fixtures
and arithmetic; it does NOT extract facts or classify free text with a model.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import combinations
import json
from pathlib import Path

from .contracts import (ArithmeticCheck, CaseRecord, Cases, Expectations, ExpectedResult,
                        Protocols, Rule, Rules, Taxonomy, aware_time)

FILES = {
    "taxonomy": "data/knowledge/taxonomy.json", "protocols": "data/knowledge/protocols.json",
    "rules": "data/knowledge/rules.json", "records": "data/inputs/cases.json",
    "references": "data/reference/expected_results.json",
}
FACTS = {
    "elapsed_minimum": {"collected_at", "centrifuged_at", "same_sample"},
    "visit_window": {"anchor_date", "actual_visit_date", "anchor_day_number"},
    "explicit_absence": {"event_present", "register_complete", "entry_present", "urgent_flag"},
    "inventory_balance": {"opening", "received", "dispensed", "returned_to_stock", "closing", "inventory_scope_confirmed"},
    "authorization": {"actor_id", "authorized_actor_id", "operation_at", "authorized_from", "authorized_to", "operation", "authorized_operations"},
    "aligned_value_match": {"source_subject_id", "edc_subject_id", "source_field", "edc_field", "source_timepoint", "edc_timepoint", "source_unit", "edc_unit", "source_value", "edc_value"},
}


def _unique(items, description):
    if len(items) != len(set(items)):
        raise ValueError(f"Duplicate {description}.")


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _read(root: str | Path, kind: str, model):
    payload = json.loads((Path(root) / FILES[kind]).read_text(encoding="utf-8"),
                         object_pairs_hook=_no_duplicate_keys)
    if payload.get("synthetic") is not True:
        raise ValueError("M1 accepts explicitly synthetic fixtures only.")
    return model.model_validate(payload)


def load_records(root: str | Path) -> list[CaseRecord]:
    """No reference or knowledge reads: only user-input fixture records."""
    records = _read(root, "records", Cases).records
    _unique([item.case_id for item in records], "case ID")
    for item in records:
        if not all(x.startswith("SYN-") for x in (item.study_id, item.site_id, item.protocol_id)):
            raise ValueError("Only fictional SYN scope identifiers belong in these fixtures.")
        if not item.text.startswith("【合成虚拟记录】"):
            raise ValueError("A demo record must visibly identify its synthetic origin.")
    return records


def build_model_input(record: CaseRecord) -> dict:
    """Input projection for M2; returns data only, never invokes a model.

    Case IDs such as DEMO-AE-01 leak the authored scenario family, so even those
    identifiers are excluded. Keep them outside the prompt for local tracking.
    """
    return {"synthetic": True, **record.model_dump(exclude={"case_id"})}


def load_references(root: str | Path) -> list[ExpectedResult]:
    """Authoring/test-only reader; must not be called by the future classifier."""
    return _read(root, "references", Expectations).results


def _parameters(rule: Rule) -> None:
    if set(rule.required_facts) != FACTS[rule.check_type] or len(rule.required_facts) != len(FACTS[rule.check_type]):
        raise ValueError(f"Missing, repeated or unexpected required fact for {rule.key}")
    p = rule.parameters
    if rule.check_type == "elapsed_minimum":
        valid = set(p) == {"minimum_minutes"} and type(p["minimum_minutes"]) is int and p["minimum_minutes"] > 0
    elif rule.check_type == "visit_window":
        valid = (set(p) == {"target_day", "early_days", "late_days", "anchor_day_number", "boundary_inclusive"}
                 and all(type(p[key]) is int for key in ("target_day", "early_days", "late_days", "anchor_day_number"))
                 and p["anchor_day_number"] == 1 and p["target_day"] > 0
                 and p["early_days"] >= 0 and p["late_days"] >= 0 and p["boundary_inclusive"] is True)
    elif rule.check_type == "explicit_absence":
        valid = set(p) == {"required_register"} and isinstance(p["required_register"], str) and bool(p["required_register"].strip())
    elif rule.check_type == "inventory_balance":
        valid = p == {"formula": "opening + received - dispensed + returned_to_stock", "quantity_type": "non_negative_integer", "alignment_required": "同一药品、中心、单位和结算期间"}
    elif rule.check_type == "authorization":
        valid = set(p) == {"authorization_interval", "require_same_actor"} and p["authorization_interval"] == "[from,to)" and p["require_same_actor"] is True
    else:
        valid = set(p) == {"comparison", "require_alignment"} and p["comparison"] == "exact" and p["require_alignment"] is True
    if not valid:
        raise ValueError(f"Unsupported or invalid parameters: {rule.key}")


def load_knowledge(root: str | Path) -> tuple[Taxonomy, Protocols, Rules]:
    """Validate source quotes, taxonomy, parameters and version links, no answers."""
    taxonomy = _read(root, "taxonomy", Taxonomy)
    protocols = _read(root, "protocols", Protocols)
    rules = _read(root, "rules", Rules)
    return validate_knowledge(taxonomy, protocols, rules)


def validate_knowledge(taxonomy: Taxonomy, protocols: Protocols, rules: Rules) -> tuple[Taxonomy, Protocols, Rules]:
    """Shared file/SQLite snapshot validation; no reference-answer access."""
    _unique([p.l3_id for p in taxonomy.paths], "L3 ID")
    _unique([(p.l1, p.l2, p.l3) for p in taxonomy.paths], "classification path")
    _unique([c.l4_id for p in taxonomy.paths for c in p.l4_candidates], "L4 ID")
    _unique([doc.document_id for doc in protocols.documents], "document ID")
    _unique([doc.protocol_id for doc in protocols.documents], "protocol ID")
    _unique([rule.key for rule in rules.rules], "rule version key")
    l3_ids = {p.l3_id for p in taxonomy.paths}
    documents = {d.document_id: d for d in protocols.documents}
    rules_by_id = {r.key: r for r in rules.rules}
    for doc in protocols.documents:
        _unique([s.section_id for s in doc.sections], "section ID within document")
    for rule in rules.rules:
        if rule.output_l3_id not in l3_ids:
            raise ValueError(f"Unknown L3 path: {rule.output_l3_id}")
        _parameters(rule)
        doc = documents.get(rule.source.document_id)
        if doc is None:
            raise ValueError("Rule source document is missing.")
        if (rule.scope.study_id, rule.scope.protocol_id, rule.scope.effective_from, rule.scope.effective_to) != (doc.study_id, doc.protocol_id, doc.effective_from, doc.effective_to):
            raise ValueError("Rule scope/version differs from its source document.")
        sections = {s.section_id: s.text for s in doc.sections}
        text = sections.get(rule.source.section_id, "")
        if text.count(rule.source.quote) != 1:
            raise ValueError("Rule quote must resolve uniquely to its exact source section.")
        # This fixture sanity check checks its explicit parameter wording. It is
        # not general NLP entailment checking or a clinical rule interpretation.
        if rule.check_type == "elapsed_minimum" and f"{rule.parameters['minimum_minutes']}分钟" not in rule.source.quote:
            raise ValueError("Fixture's minimum-time parameter has no matching source wording.")
        _unique(rule.supersedes, "superseded rule key")
        for old_key in rule.supersedes:
            old = rules_by_id.get(old_key)
            if old is None or old.rule_id != rule.rule_id or old.key == rule.key:
                raise ValueError("Supersedes must identify an existing older version of this rule.")
            if (old.scope.study_id != rule.scope.study_id or old.scope.site_id != rule.scope.site_id
                    or old.output_l3_id != rule.output_l3_id or old.check_type != rule.check_type):
                raise ValueError("A version update cannot silently replace a different rule scope/family.")
            if old.scope.effective_to is None or aware_time(old.scope.effective_to) > aware_time(rule.scope.effective_from):
                raise ValueError("Version replacement must not overlap an old open effective interval.")
    return taxonomy, protocols, rules


def applicable(rule: Rule, record: CaseRecord) -> bool:
    scope = rule.scope
    return ((scope.study_id, scope.site_id, scope.protocol_id) == (record.study_id, record.site_id, record.protocol_id)
            and aware_time(scope.effective_from) <= aware_time(record.event_at)
            and (scope.effective_to is None or aware_time(record.event_at) < aware_time(scope.effective_to)))


def rule_conflicts(rules: list[Rule]) -> list[tuple[str, str]]:
    """Detect incompatible overlapping same-scope/check policies, not prose conflicts."""
    result = []
    for a, b in combinations(rules, 2):
        same_scope = (a.scope.study_id, a.scope.site_id, a.scope.protocol_id, a.check_type) == (b.scope.study_id, b.scope.site_id, b.scope.protocol_id, b.check_type)
        overlap = ((a.scope.effective_to is None or aware_time(b.scope.effective_from) < aware_time(a.scope.effective_to))
                   and (b.scope.effective_to is None or aware_time(a.scope.effective_from) < aware_time(b.scope.effective_to)))
        incompatible = (a.parameters, a.output_l3_id, a.risk, a.suggested_decision) != (b.parameters, b.output_l3_id, b.risk, b.suggested_decision)
        if same_scope and overlap and incompatible:
            result.append((a.key, b.key))
    return result


def arithmetic_value(check: ArithmeticCheck) -> Decimal:
    """Recompute AUTHOR-SUPPLIED operands, not facts extracted from raw records."""
    operands = check.operands
    keys = {"elapsed_minutes": {"start", "end"}, "visit_day": {"anchor_date", "actual_visit_date", "anchor_day_number"},
            "inventory_closing": {"opening", "received", "dispensed", "returned_to_stock"}}
    if set(operands) != keys[check.operation]:
        raise ValueError("Arithmetic operands do not match the declared operation.")
    if check.operation == "elapsed_minutes":
        if any(type(value) is not str for value in operands.values()):
            raise ValueError("Timestamp operands must be explicit ISO strings.")
        duration = aware_time(operands["end"]) - aware_time(operands["start"])
        if duration.total_seconds() < 0:
            raise ValueError("Negative duration requires investigation, not normal classification.")
        return Decimal(str(duration.total_seconds())) / Decimal(60)
    if check.operation == "visit_day":
        if (type(operands["anchor_day_number"]) is not int
                or type(operands["anchor_date"]) is not str or type(operands["actual_visit_date"]) is not str):
            raise ValueError("Day number must be an integer.")
        days = (date.fromisoformat(operands["actual_visit_date"]) - date.fromisoformat(operands["anchor_date"])).days
        if days < 0 or operands["anchor_day_number"] != 1:
            raise ValueError("This demo uses start day 1 and nonnegative elapsed days.")
        return Decimal(days + operands["anchor_day_number"])
    if any(type(value) is not int or value < 0 for value in operands.values()):
        raise ValueError("All inventory quantities must be nonnegative integers, not missing/boolean.")
    value = operands["opening"] + operands["received"] - operands["dispensed"] + operands["returned_to_stock"]
    if value < 0:
        raise ValueError("Negative inventory balance is not a normal quantity.")
    return Decimal(value)


def validate_bundle(root: str | Path) -> dict:
    """Cross-check authored M1 data. This is NOT classification/model evaluation."""
    taxonomy, protocols, rules = load_knowledge(root)
    conflicts = rule_conflicts(rules.rules)
    allowed_conflicts = {frozenset({"SYN-RULE-PK-CONFLICT-A@1", "SYN-RULE-PK-CONFLICT-B@1"})}
    if {frozenset(pair) for pair in conflicts} != allowed_conflicts:
        raise ValueError("Only the explicitly authored conflict-study fixture may have conflicting policies.")
    records, references = load_records(root), load_references(root)
    _unique([r.case_id for r in references], "reference case ID")
    records_by_id = {r.case_id: r for r in records}
    if set(records_by_id) != {r.case_id for r in references}:
        raise ValueError("Every input case must have exactly one separate authored reference.")
    documents = {d.protocol_id: d for d in protocols.documents}
    by_rule = {r.key: r for r in rules.rules}
    by_path = {p.l3_id: p for p in taxonomy.paths}
    all_finding_ids = []
    checks = []
    for ref in references:
        record = records_by_id[ref.case_id]
        doc = documents.get(record.protocol_id)
        if doc is None or doc.study_id != record.study_id:
            raise ValueError("Input references an unavailable/mismatched synthetic study protocol.")
        if (aware_time(record.event_at) < aware_time(doc.effective_from)
                or (doc.effective_to is not None and aware_time(record.event_at) >= aware_time(doc.effective_to))):
            raise ValueError("Input event is outside the selected protocol version's effective interval.")
        _unique(ref.relevant_rule_keys, "reference relevant rule")
        for quote in ref.record_quotes + [q for f in ref.findings for q in f.record_quotes]:
            if record.text.count(quote) != 1:
                raise ValueError(f"Reference record quote is absent or ambiguous: {ref.case_id}: {quote}")
        selected = []
        for key in ref.relevant_rule_keys:
            rule = by_rule.get(key)
            if rule is None or not applicable(rule, record):
                raise ValueError(f"Reference selects unknown or inapplicable rule: {ref.case_id} / {key}")
            selected.append(rule)
        found_conflicts = rule_conflicts(selected)
        if (ref.status == "rule_conflict") != bool(found_conflicts):
            raise ValueError("Expected rule-conflict status must agree with selected rule policies.")
        for finding in ref.findings:
            all_finding_ids.append(finding.finding_id)
            if finding.l3_id not in by_path:
                raise ValueError("Reference has an invented L3.")
            _unique(finding.rule_keys, "finding rule")
            if not set(finding.rule_keys) <= set(ref.relevant_rule_keys):
                raise ValueError("Finding cites a rule outside its reference's selected rules.")
            for key in finding.rule_keys:
                rule = by_rule[key]
                if (finding.l3_id, finding.risk, finding.suggested_decision) != (rule.output_l3_id, rule.risk, rule.suggested_decision):
                    raise ValueError("Reference finding differs from its explicit demo rule policy.")
        for check in ref.arithmetic_checks:
            actual = arithmetic_value(check)
            if actual != Decimal(check.expected_value):
                raise ValueError(f"Incorrect authored arithmetic: {ref.case_id}")
            checks.append({"case_id": ref.case_id, "operation": check.operation, "value": str(actual),
                           "source": "author-supplied reference operands; no NLP extraction"})
    _unique(all_finding_ids, "finding ID")
    return {"stage": "M1", "synthetic": True, "mode": "fixture_validation_not_model_evaluation",
            "case_count": len(records), "l3_path_count": len(taxonomy.paths),
            "document_count": len(protocols.documents), "rule_version_count": len(rules.rules),
            "authored_finding_count": len(all_finding_ids), "arithmetic_checks": checks,
            "intentional_rule_conflicts": conflicts,
            "model_calls": 0, "clinical_accuracy_measured": False,
            "limitation": "Exact source quotes and arithmetic do not prove semantic extraction or clinical correctness."}


def preview_case(root: str | Path, case_id: str) -> dict:
    """Explicit answer-bearing teaching preview; never use as classifier output."""
    validate_bundle(root)
    taxonomy, _, rules = load_knowledge(root)
    record = next((r for r in load_records(root) if r.case_id == case_id), None)
    ref = next((r for r in load_references(root) if r.case_id == case_id), None)
    if record is None or ref is None:
        raise ValueError(f"Unknown demonstration case: {case_id}")
    paths, by_rule = {p.l3_id: p for p in taxonomy.paths}, {r.key: r for r in rules.rules}
    return {"mode": "fixture_preview", "notice": "预期答案预览：没有调用模型，也不是模型预测。",
            "case_id": case_id, "input": build_model_input(record), "expected_status": ref.status,
            "expected_findings": [{"finding_id": f.finding_id, "一级分类": paths[f.l3_id].l1,
                                   "二级分类": paths[f.l3_id].l2, "三级分类": paths[f.l3_id].l3,
                                   "风险等级": f.risk, "建议判定": f.suggested_decision,
                                   "是否需人工复核": f.requires_human_review,
                                   "建议措施": [by_rule[key].suggested_action for key in f.rule_keys],
                                   "原记录证据": f.record_quotes,
                                   "规则证据": [by_rule[key].source.model_dump() for key in f.rule_keys]}
                                  for f in ref.findings], "explanation": ref.explanation,
            "missing_facts": ref.missing_facts, "triage_priority": ref.triage_priority,
            "arithmetic": [{"operation": c.operation, "value": str(arithmetic_value(c))} for c in ref.arithmetic_checks]}
