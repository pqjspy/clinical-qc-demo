"""Strict M1 input, knowledge and authored-expectation contracts.

Parsing validates structure, never clinical correctness. All data are synthetic.
Cross-file IDs, evidence and rule applicability are checked in data.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1)]
Risk = Literal["低", "中", "高"]
Status = Literal["proposed_findings", "no_finding_for_checked_rule", "needs_information",
                 "rule_conflict", "out_of_scope", "ambiguous_evidence"]


def aware_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Explicit timezone required; do not assume the local machine timezone.")
    return parsed


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Envelope(StrictModel):
    schema_version: Literal["m1-v1"]
    synthetic: Literal[True]


class L4Candidate(StrictModel):
    l4_id: Text
    label: Text


class TaxonomyPath(StrictModel):
    l3_id: Text
    l1: Text
    l2: Text
    l3: Text
    l4_candidates: list[L4Candidate] = Field(min_length=1)
    description: Text


class Taxonomy(Envelope):
    taxonomy_id: Text
    paths: list[TaxonomyPath] = Field(min_length=1)


class Section(StrictModel):
    section_id: Text
    text: Text


class ProtocolDocument(StrictModel):
    document_id: Text
    study_id: Text
    protocol_id: Text
    version: Text
    effective_from: Text
    effective_to: Text | None
    sections: list[Section] = Field(min_length=1)

    @model_validator(mode="after")
    def dates(self):
        start = aware_time(self.effective_from)
        if self.effective_to is not None and aware_time(self.effective_to) <= start:
            raise ValueError("Protocol interval must be nonempty [from,to).")
        return self


class Protocols(Envelope):
    documents: list[ProtocolDocument] = Field(min_length=1)


class RuleScope(StrictModel):
    study_id: Text
    site_id: Text
    protocol_id: Text
    effective_from: Text
    effective_to: Text | None

    @model_validator(mode="after")
    def dates(self):
        start = aware_time(self.effective_from)
        if self.effective_to is not None and aware_time(self.effective_to) <= start:
            raise ValueError("Rule interval must be nonempty [from,to).")
        return self


class SourceQuote(StrictModel):
    document_id: Text
    section_id: Text
    quote: Text


class Rule(StrictModel):
    rule_id: Text
    version: Text
    scope: RuleScope
    publication_status: Literal["demo_approved"]
    check_type: Literal["elapsed_minimum", "visit_window", "explicit_absence", "inventory_balance",
                        "authorization", "aligned_value_match"]
    required_facts: list[Text] = Field(min_length=1)
    parameters: dict[str, str | int | float | bool]
    output_l3_id: Text
    risk: Risk
    suggested_decision: Literal["方案偏离", "需人工判定"]
    suggested_action: Text
    requires_human_review: Literal[True]
    source: SourceQuote
    supersedes: list[Text]

    @property
    def key(self) -> str:
        return f"{self.rule_id}@{self.version}"


class Rules(Envelope):
    rules: list[Rule] = Field(min_length=1)


class CaseRecord(StrictModel):
    case_id: Text
    study_id: Text
    site_id: Text
    protocol_id: Text
    event_at: Text
    timezone: Literal["Asia/Shanghai"]
    text: Text

    @model_validator(mode="after")
    def date(self):
        aware_time(self.event_at)
        return self


class Cases(Envelope):
    records: list[CaseRecord] = Field(min_length=1)


class ArithmeticCheck(StrictModel):
    operation: Literal["elapsed_minutes", "visit_day", "inventory_closing"]
    operands: dict[str, str | int]
    expected_value: int
    note: Text


class ExpectedFinding(StrictModel):
    finding_id: Text
    l3_id: Text
    risk: Risk
    suggested_decision: Literal["方案偏离", "需人工判定"]
    record_quotes: list[Text] = Field(min_length=1)
    rule_keys: list[Text] = Field(min_length=1)
    requires_human_review: Literal[True]


class ExpectedResult(StrictModel):
    case_id: Text
    scenario_group: Text
    basis: Literal["authored_synthetic_expectation_not_model_prediction"]
    status: Status
    findings: list[ExpectedFinding]
    relevant_rule_keys: list[Text]
    record_quotes: list[Text] = Field(min_length=1)
    missing_facts: list[Text]
    explanation: Text
    review_required: Literal[True]
    triage_priority: Literal["routine", "priority"]
    arithmetic_checks: list[ArithmeticCheck]

    @model_validator(mode="after")
    def outcome(self):
        if (self.status == "proposed_findings") != bool(self.findings):
            raise ValueError("Only proposed_findings carries classified findings; unresolved is not a class.")
        if self.status == "needs_information" and not self.missing_facts:
            raise ValueError("Missing-information outcome must identify the missing facts.")
        if self.status == "out_of_scope" and self.relevant_rule_keys:
            raise ValueError("Out-of-scope fixture cannot assert an applicable rule.")
        if any(item.risk == "高" for item in self.findings) and self.triage_priority != "priority":
            raise ValueError("A known high-risk finding cannot receive routine triage.")
        return self


class Expectations(Envelope):
    usage: Literal["demonstration_fixtures_not_independent_evaluation"]
    results: list[ExpectedResult] = Field(min_length=1)
