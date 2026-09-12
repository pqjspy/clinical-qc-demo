"""PK evidence/rule unit tests using authored test doubles, not model scores.

These tests load only user-input and knowledge files. PKExtraction objects are
deliberately mocked boundary probes; no LLM or reference-answer file is used.
"""
from copy import deepcopy
import builtins
import io
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinical_qc_demo.contracts import CaseRecord, Rule, Taxonomy
from clinical_qc_demo.data import load_knowledge, load_records
from clinical_qc_demo.m2_contracts import PKExtraction
from clinical_qc_demo.pk_check import EvidenceError, check_pk, validate_extraction


class PKTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.taxonomy, _, knowledge = load_knowledge(ROOT)
        cls.rules = knowledge.rules
        cls.records = {record.case_id: record for record in load_records(ROOT)}
        cls.pk_v1 = next(rule for rule in cls.rules if rule.key == "SYN-RULE-PK-001@1")
        cls.pk_v2 = next(rule for rule in cls.rules if rule.key == "SYN-RULE-PK-001@2")

    def record(self, *, text=None, **updates):
        data = self.records["QC002"].model_dump()
        if text is not None:
            data["text"] = text
        data.update(updates)
        return CaseRecord.model_validate(data)

    def extraction(self, **updates):
        data = {
            "scope": "single_pk", "scope_quote": "PK样本",
            "same_sample_quote": "同一份PK样本", "date_quote": "2026-09-01",
            "timezone_quote": "北京时间", "collected_time_quote": "采血时间为10:06",
            "centrifuged_time_quote": "开始离心时间为10:33",
            "other_issue_quotes": [], "uncertainty_quotes": [],
        }
        data.update(updates)
        return PKExtraction.model_validate(data)

    def plain(self, *, start="10:06", end="10:33", day="2026-09-01", prefix="同一份PK样本", suffix="", **updates):
        text = f"【合成虚拟记录】{day}，{prefix}，采血时间为{start}，开始离心时间为{end}，均为北京时间。{suffix}"
        record = self.record(text=text, event_at=f"{day}T12:00:00+08:00", **updates)
        extraction = self.extraction(date_quote=day, collected_time_quote=f"采血时间为{start}",
                                     centrifuged_time_quote=f"开始离心时间为{end}")
        return record, extraction

    def evaluate(self, record, extraction, *, candidates=None, rules=None, taxonomy=None):
        return check_pk(record, validate_extraction(record, extraction),
                        [self.pk_v1] if candidates is None else candidates,
                        self.rules if rules is None else rules,
                        self.taxonomy if taxonomy is None else taxonomy)

    def assert_refused(self, record, extraction):
        try:
            validated = validate_extraction(record, extraction)
        except EvidenceError:
            return
        self.assertNotEqual(validated["status"], "ready")
        self.assertIsNone(validated["facts"])

    def test_qc002_real_input_mock_extraction_and_rule_derived_path(self):
        record = self.records["QC002"]
        result = self.evaluate(record, self.extraction())
        self.assertEqual(result["status"], "proposed_findings")
        self.assertEqual(result["calculation"]["actual_minutes"], "27")
        self.assertEqual(result["calculation"]["minimum_minutes"], 30)
        finding = result["findings"][0]
        self.assertEqual((finding["l1"], finding["l2"], finding["l3"]),
                         ("样本管理", "PK 样本管理", "样本处理时间不足"))
        self.assertEqual(finding["l3_id"], self.pk_v1.output_l3_id)
        self.assertEqual(finding["risk"], self.pk_v1.risk)
        self.assertEqual(finding["suggested_decision"], self.pk_v1.suggested_decision)
        self.assertEqual(finding["suggested_action"], self.pk_v1.suggested_action)
        self.assertTrue(finding["requires_human_review"])
        self.assertTrue(result["review_required"])

    def test_27_30_40_minute_boundaries_do_not_fabricate_normal_finding(self):
        for end, minutes, hit in (("10:33", "27", True), ("10:36", "30", False), ("10:46", "40", False)):
            with self.subTest(end=end):
                record, extraction = self.plain(end=end)
                result = self.evaluate(record, extraction)
                self.assertEqual(result["calculation"]["actual_minutes"], minutes)
                self.assertEqual(result["calculation"]["hit"], hit)
                self.assertEqual(bool(result["findings"]), hit)
                self.assertEqual(result["status"], "proposed_findings" if hit else "no_finding_for_checked_rule")
                self.assertTrue(result["review_required"])
                if not hit:
                    self.assertNotEqual(result["risk"], "低")

    def test_every_record_quote_has_exact_half_open_offsets(self):
        record = self.records["QC002"]
        result = validate_extraction(record, self.extraction())
        ids = []
        for item in result["evidence"]:
            self.assertEqual(record.text[item["start_char"]:item["end_char"]], item["quote"])
            ids.append(item["evidence_id"])
        self.assertEqual(len(ids), len(set(ids)))
        checked = self.evaluate(record, self.extraction())
        cited = [item for item in checked["evidence"] if item["origin"] == "rule"]
        self.assertEqual(cited[0]["quote"], self.pk_v1.source.quote)
        self.assertEqual(cited[0]["rule_key"], self.pk_v1.key)

    def test_fabricated_and_nonunique_quotes_are_rejected(self):
        record, extraction = self.plain()
        for field, quote in (("collected_time_quote", "采血时间为08:00"),
                             ("timezone_quote", "UTC"), ("same_sample_quote", "同一份血液样本")):
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                validate_extraction(record, self.extraction(**{field: quote}))
        repeated = self.record(text=record.text + "补充说明：北京时间。")
        with self.assertRaises(EvidenceError):
            validate_extraction(repeated, extraction)
        with self.assertRaises(EvidenceError):
            validate_extraction(record, self.extraction(uncertainty_quotes=["不存在于原文的矛盾"]))

    def test_swapping_collected_and_centrifuged_roles_is_rejected(self):
        with self.assertRaises(EvidenceError):
            validate_extraction(self.records["QC002"], self.extraction(
                collected_time_quote="开始离心时间为10:33", centrifuged_time_quote="采血时间为10:06"))

    def test_bare_clock_cannot_count_as_role_evidence(self):
        for update in ({"collected_time_quote": "10:06"}, {"centrifuged_time_quote": "10:33"}):
            with self.subTest(update=update), self.assertRaises(EvidenceError):
                validate_extraction(self.records["QC002"], self.extraction(**update))

    def test_submission_time_is_not_missing_centrifuged_time(self):
        text = "【合成虚拟记录】2026-09-01，同一份PK样本，采血时间为10:06，开始离心时间未提供。提交时间为10:33，均为北京时间。"
        record = self.record(text=text)
        missing = self.extraction(centrifuged_time_quote=None)
        result = self.evaluate(record, missing)
        self.assertEqual(result["status"], "needs_information")
        self.assertEqual(result["findings"], [])
        self.assertIsNone(result["calculation"])
        with self.assertRaises(EvidenceError):
            validate_extraction(record, self.extraction(centrifuged_time_quote="提交时间为10:33"))
        with self.assertRaises(EvidenceError):
            validate_extraction(record, self.extraction(centrifuged_time_quote="开始离心时间未提供。提交时间为10:33"))

    def test_same_sample_negated_outside_model_chosen_substring_is_not_ready(self):
        for negated in ("这不是同一份PK样本", "并非同一份PK样本", "无法确认同一份PK样本"):
            with self.subTest(negated=negated):
                record, extraction = self.plain(prefix=negated)
                self.assert_refused(record, extraction)

    def test_event_role_negated_outside_model_chosen_substring_is_not_ready(self):
        record, extraction = self.plain()
        record = self.record(text=record.text.replace("采血时间为10:06", "并非采血时间为10:06"))
        self.assert_refused(record, extraction)

    def test_explicit_role_negation_inside_quote_is_rejected(self):
        record, _ = self.plain()
        record = self.record(text=record.text.replace("开始离心时间为10:33", "开始离心时间不是10:33"))
        with self.assertRaises(EvidenceError):
            validate_extraction(record, self.extraction(centrifuged_time_quote="开始离心时间不是10:33"))

    def test_unreported_additional_or_duplicate_times_force_review(self):
        for suffix in ("另一个台账记载时间10:36。", "记录提交时间12:00。", "复核时重复记载10:33。"):
            with self.subTest(suffix=suffix):
                record, extraction = self.plain(suffix=suffix)
                result = self.evaluate(record, extraction)
                self.assertEqual(result["status"], "ambiguous_evidence")
                self.assertEqual(result["findings"], [])
                self.assertIsNone(result["calculation"])

    def test_known_multiple_issue_text_cannot_be_dropped_by_model(self):
        record = self.records["DEMO-MULTI-01"]
        extraction = self.extraction(collected_time_quote="采血于10:06", centrifuged_time_quote="开始离心于10:33")
        result = self.evaluate(record, extraction)
        self.assertEqual(result["status"], "unsupported_scope")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["triage_priority"], "priority")
        self.assertTrue(result["review_required"])
        self.assertIsNone(result["calculation"])

    def test_model_declared_other_issue_transfers_whole_record(self):
        record, extraction = self.plain(suffix="另一问题有待核对。")
        extraction = self.extraction(other_issue_quotes=["另一问题有待核对"])
        result = self.evaluate(record, extraction)
        self.assertEqual(result["status"], "unsupported_scope")
        self.assertEqual(result["findings"], [])

    def test_two_dates_relative_days_and_negative_order_are_not_assumed(self):
        for suffix in ("本记录还涉及2026-09-02。", "离心实际为次日。", "开始时间尚未确定。"):
            with self.subTest(suffix=suffix):
                record, extraction = self.plain(suffix=suffix)
                self.assert_refused(record, extraction)
        record, extraction = self.plain(start="23:50", end="00:20")
        self.assert_refused(record, extraction)

    def test_context_day_mismatch_is_not_ignored(self):
        record = self.record(event_at="2026-09-02T10:33:00+08:00")
        result = self.evaluate(record, self.extraction())
        self.assertEqual(result["status"], "ambiguous_evidence")
        self.assertEqual(result["findings"], [])
        # A different offset denoting the same Shanghai calendar day is valid.
        same_day = self.record(event_at="2026-09-01T02:33:00+00:00")
        self.assertEqual(validate_extraction(same_day, self.extraction())["status"], "ready")

    def test_missing_timezone_date_or_sample_identity_requests_information(self):
        for field in ("timezone_quote", "date_quote", "same_sample_quote", "collected_time_quote"):
            with self.subTest(field=field):
                result = self.evaluate(self.records["QC002"], self.extraction(**{field: None}))
                self.assertEqual(result["status"], "needs_information")
                self.assertEqual(result["findings"], [])
                self.assertIn(field, result["missing_facts"])

    def test_rule_absence_and_incomplete_retrieval_do_not_mean_normal(self):
        record, extraction = self.plain()
        for candidates, rules in (([], []), ([], self.rules), ([self.rules[1]], self.rules)):
            with self.subTest(candidates=candidates):
                result = self.evaluate(record, extraction, candidates=candidates, rules=rules)
                self.assertEqual(result["status"], "rule_not_found")
                self.assertEqual(result["findings"], [])
                self.assertEqual(result["risk"], "待定")
                self.assertIsNone(result["calculation"])

    def test_foreign_study_or_site_never_borrows_normal_rule(self):
        for updates in ({"study_id": "SYN-ANOTHER-STUDY"}, {"site_id": "SYN-ANOTHER-SITE"}):
            with self.subTest(updates=updates):
                record = self.record(**updates)
                result = self.evaluate(record, self.extraction())
                self.assertEqual(result["status"], "rule_not_found")
                self.assertEqual(result["findings"], [])

    def test_conflict_all_rules_cannot_be_hidden_by_one_ranked_candidate(self):
        record, extraction = self.plain(study_id="SYN-STUDY-CONFLICT", protocol_id="SYN-PROTOCOL-CONFLICT-v1")
        candidate = next(rule for rule in self.rules if rule.key == "SYN-RULE-PK-CONFLICT-B@1")
        result = self.evaluate(record, extraction, candidates=[candidate])
        self.assertEqual(result["status"], "rule_conflict")
        self.assertEqual(result["findings"], [])
        self.assertIsNone(result["calculation"])
        self.assertEqual({key for pair in result["conflicts"] for key in pair},
                         {"SYN-RULE-PK-CONFLICT-A@1", "SYN-RULE-PK-CONFLICT-B@1"})

    def test_v1_v2_policies_and_exact_effective_boundary(self):
        old_record, old_extraction = self.plain(day="2026-09-30")
        new_record, new_extraction = self.plain(day="2026-10-01", protocol_id="SYN-PROTOCOL-001-v2")
        old = self.evaluate(old_record, old_extraction)
        new = self.evaluate(new_record, new_extraction, candidates=[self.pk_v2])
        self.assertEqual(old["calculation"]["minimum_minutes"], 30)
        self.assertEqual(old["status"], "proposed_findings")
        self.assertEqual(new["calculation"]["minimum_minutes"], 20)
        self.assertEqual(new["status"], "no_finding_for_checked_rule")
        self.assertEqual(new["rule_keys"], ["SYN-RULE-PK-001@2"])
        expired, extraction = self.plain(day="2026-10-01")
        self.assertEqual(self.evaluate(expired, extraction)["status"], "rule_not_found")

    def test_intra_day_effective_boundary_must_cover_both_events(self):
        # Authored in-memory rule variation isolates scope handling; not a new
        # published knowledge file or a claim about an actual clinical policy.
        rule_data = self.pk_v1.model_dump()
        rule_data["scope"]["effective_from"] = "2026-09-01T10:20:00+08:00"
        shifted = Rule.model_validate(rule_data)
        record, extraction = self.plain()
        result = self.evaluate(record, extraction, candidates=[shifted], rules=[shifted])
        self.assertEqual(result["status"], "ambiguous_evidence")
        self.assertEqual(result["findings"], [])

    def test_candidate_with_canonical_key_cannot_replace_policy(self):
        altered = self.pk_v1.model_dump()
        altered["parameters"]["minimum_minutes"] = 20
        altered["risk"] = "低"
        counterfeit_candidate = Rule.model_validate(altered)
        try:
            result = self.evaluate(self.records["QC002"], self.extraction(), candidates=[counterfeit_candidate])
        except EvidenceError:
            return  # Explicit rejection is an acceptable fail-closed contract.
        self.assertEqual(result["status"], "proposed_findings")
        self.assertEqual(result["calculation"]["minimum_minutes"], 30)
        self.assertEqual(result["findings"][0]["risk"], "中")

    def test_legitimate_canonical_policy_controls_risk_and_l3(self):
        altered = self.pk_v1.model_dump()
        altered["risk"] = "高"
        canonical = Rule.model_validate(altered)
        result = self.evaluate(self.records["QC002"], self.extraction(), candidates=[canonical], rules=[canonical])
        self.assertEqual(result["findings"][0]["risk"], "高")
        self.assertEqual(result["triage_priority"], "priority")
        self.assertEqual(result["findings"][0]["l3_id"], canonical.output_l3_id)
        data = self.taxonomy.model_dump()
        data["paths"] = [path for path in data["paths"] if path["l3_id"] != canonical.output_l3_id]
        with self.assertRaises(EvidenceError):
            self.evaluate(self.records["QC002"], self.extraction(), candidates=[canonical], rules=[canonical], taxonomy=Taxonomy.model_validate(data))

    def test_no_answer_file_model_network_or_input_mutation(self):
        record = self.records["QC002"]
        extraction = self.extraction()
        before = deepcopy(record.model_dump()), deepcopy(extraction.model_dump()), [r.model_dump() for r in self.rules]
        with patch.object(Path, "read_text", side_effect=AssertionError("No reference/file reads")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("No file reads")), \
                patch.object(builtins, "open", side_effect=AssertionError("No reference/file reads")), \
                patch.object(io, "open", side_effect=AssertionError("No file reads")), \
                patch.object(socket, "socket", side_effect=AssertionError("No model/network calls")):
            result = self.evaluate(record, extraction)
        self.assertEqual(result["calculation"]["actual_minutes"], "27")
        self.assertEqual(before, (record.model_dump(), extraction.model_dump(), [r.model_dump() for r in self.rules]))


if __name__ == "__main__":
    unittest.main()
