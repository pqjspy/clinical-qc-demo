"""M2 is a narrow single-sample, same-day PK demonstration, not six-family QC."""
from typing import Literal

from pydantic import Field

from .contracts import StrictModel


class PKExtraction(StrictModel):
    """LLM selects verbatim spans; Python computes offsets and normalizes values."""
    scope: Literal["single_pk", "unsupported_or_multiple", "uncertain"]
    scope_quote: str | None = Field(max_length=32)
    same_sample_quote: str | None
    date_quote: str | None = Field(max_length=10)
    timezone_quote: str | None
    collected_time_quote: str | None
    centrifuged_time_quote: str | None
    other_issue_quotes: list[str]
    uncertainty_quotes: list[str]


class ExplanationProposal(StrictModel):
    status: Literal["proposed_findings", "no_finding_for_checked_rule"]
    l3_id: str | None
    rule_keys: list[str]
    evidence_ids: list[str]
    explanation: str = Field(min_length=1, max_length=1000)
