"""Independent fixture/contract tests: no LLM or clinical-accuracy claims."""
from __future__ import annotations

import builtins
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pydantic import ValidationError
from clinical_qc_demo.contracts import ArithmeticCheck, CaseRecord, ExpectedFinding, ExpectedResult, Rule, TaxonomyPath
from clinical_qc_demo.data import (FILES, applicable, arithmetic_value, build_model_input, load_knowledge,
                                  load_records, load_references, preview_case, rule_conflicts, validate_bundle)


class M1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.taxonomy, cls.protocols, cls.rules = load_knowledge(ROOT)
        cls.records = {r.case_id: r for r in load_records(ROOT)}
        cls.references = {r.case_id: r for r in load_references(ROOT)}

    def mutated(self, kind, change, operation=validate_bundle):
        with tempfile.TemporaryDirectory(prefix="clinical-qc-test-") as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "data", root / "data")
            file = root / FILES[kind]
            data = json.loads(file.read_text())
            change(data)
            file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return operation(root)

    def test_counts_and_no_model_metrics(self):
        result = validate_bundle(ROOT)
        self.assertEqual((result["case_count"], result["l3_path_count"], result["document_count"], result["rule_version_count"]), (12, 6, 3, 9))
        self.assertEqual(result["authored_finding_count"], 8)
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(result["clinical_accuracy_measured"])
        self.assertNotIn("f1", result)

    def test_qc002_exact_path_evidence_and_arithmetic(self):
        actual = preview_case(ROOT, "QC002")
        row = actual["expected_findings"][0]
        self.assertEqual([row[k] for k in ["一级分类", "二级分类", "三级分类"]], ["样本管理", "PK 样本管理", "样本处理时间不足"])
        self.assertEqual(row["风险等级"], "中")
        self.assertEqual(row["建议判定"], "方案偏离")
        self.assertTrue(row["是否需人工复核"])
        self.assertEqual(Decimal(actual["arithmetic"][0]["value"]), Decimal(27))
        self.assertEqual(actual["mode"], "fixture_preview")
        self.assertIn("没有调用模型", actual["notice"])
        self.assertIn("至少为30分钟", row["规则证据"][0]["quote"])

    def test_boundary_is_not_a_fabricated_low_risk_finding(self):
        ref = self.references["DEMO-BOUNDARY-01"]
        self.assertEqual(ref.status, "no_finding_for_checked_rule")
        self.assertEqual(ref.findings, [])
        self.assertEqual(arithmetic_value(ref.arithmetic_checks[0]), 30)

    def test_missing_submission_time_is_not_centrifuge_time(self):
        ref = self.references["DEMO-MISSING-01"]
        self.assertEqual(ref.status, "needs_information")
        self.assertEqual(ref.missing_facts, ["centrifuged_at"])
        self.assertEqual(ref.arithmetic_checks, [])
        self.assertEqual(ref.findings, [])
        self.assertIn("提交时间不是开始离心时间", self.records[ref.case_id].text)

    def test_multifinding_retains_secondary_high_risk(self):
        ref = self.references["DEMO-MULTI-01"]
        self.assertEqual({f.l3_id for f in ref.findings}, {"L3-PK-001", "L3-AE-001"})
        self.assertEqual({f.risk for f in ref.findings}, {"中", "高"})
        self.assertEqual(ref.triage_priority, "priority")

    def test_ambiguous_evidence_is_not_edc_or_automatic_sample_violation(self):
        ref = self.references["DEMO-AMBIGUOUS-01"]
        self.assertEqual(ref.status, "ambiguous_evidence")
        self.assertEqual(ref.findings, [])
        self.assertEqual(ref.arithmetic_checks, [])
        self.assertIn("10:33", ref.record_quotes[0])
        self.assertIn("10:36", ref.record_quotes[0])

    def test_unknown_has_no_rule_or_l3(self):
        ref = self.references["DEMO-UNKNOWN-01"]
        self.assertEqual(ref.status, "out_of_scope")
        self.assertEqual(ref.relevant_rule_keys, [])
        self.assertEqual(ref.findings, [])

    def test_visit_and_inventory_independent_calculation(self):
        self.assertEqual((datetime(2026, 9, 6) - datetime(2026, 8, 1)).days + 1, 37)
        self.assertEqual(arithmetic_value(self.references["DEMO-VISIT-01"].arithmetic_checks[0]), 37)
        self.assertEqual(arithmetic_value(self.references["DEMO-DRUG-01"].arithmetic_checks[0]), 100 + 20 - 15 + 5)

    def test_scope_and_half_open_version_boundaries(self):
        by_key = {r.key: r for r in self.rules.rules}
        first, second = by_key["SYN-RULE-PK-001@1"], by_key["SYN-RULE-PK-001@2"]
        base = self.records["QC002"].model_dump()
        self.assertTrue(applicable(first, CaseRecord.model_validate(base)))
        base["event_at"] = "2026-10-01T00:00:00+08:00"
        self.assertFalse(applicable(first, CaseRecord.model_validate(base)))
        self.assertFalse(applicable(second, CaseRecord.model_validate(base)))
        base["protocol_id"] = "SYN-PROTOCOL-001-v2"
        self.assertTrue(applicable(second, CaseRecord.model_validate(base)))
        base["site_id"] = "SYN-SITE-OTHER"
        self.assertFalse(applicable(second, CaseRecord.model_validate(base)))
        self.assertEqual(first.parameters["minimum_minutes"], 30)
        self.assertEqual(second.parameters["minimum_minutes"], 20)

    def test_conflict_is_explicit_and_separate_from_normal_study(self):
        pairs = rule_conflicts(self.rules.rules)
        self.assertEqual(pairs, [("SYN-RULE-PK-CONFLICT-A@1", "SYN-RULE-PK-CONFLICT-B@1")])
        normal = [r for r in self.rules.rules if applicable(r, self.records["QC002"])]
        self.assertEqual(len(normal), 6)
        self.assertEqual(rule_conflicts(normal), [])
        ref = self.references["DEMO-CONFLICT-01"]
        self.assertEqual(ref.status, "rule_conflict")
        self.assertEqual(ref.findings, [])

    def test_model_input_is_answer_free_even_scenario_id_removed(self):
        original = self.records["DEMO-AE-01"].model_dump()
        model_input = build_model_input(self.records["DEMO-AE-01"])
        self.assertEqual(set(model_input), {"synthetic", "study_id", "site_id", "protocol_id", "event_at", "timezone", "text"})
        self.assertNotIn("DEMO-AE-01", json.dumps(model_input))
        self.assertNotIn("expected", json.dumps(model_input))
        self.assertEqual(original, self.records["DEMO-AE-01"].model_dump())

    def test_input_and_knowledge_readers_never_open_reference_answers(self):
        original_open = Path.open
        seen = []
        def guarded(path, *args, **kwargs):
            seen.append(str(path))
            if "reference" in Path(path).parts:
                raise AssertionError("Answer leakage: reference read on input path")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            for record in load_records(ROOT):
                build_model_input(record)
            load_knowledge(ROOT)
        self.assertTrue(seen)

    def test_read_only_offline_validation(self):
        paths = list((ROOT / "data").rglob("*.json"))
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        original_open, original_import = Path.open, builtins.__import__
        def guarded_open(path, mode="r", *args, **kwargs):
            if any(char in mode for char in "wax+"):
                raise AssertionError("Unexpected artifact write")
            return original_open(path, mode, *args, **kwargs)
        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"torch", "transformers", "sentence_transformers", "ollama", "openai", "requests", "httpx"}:
                raise AssertionError("Unexpected model/network dependency")
            return original_import(name, *args, **kwargs)
        with patch.object(Path, "open", guarded_open), patch("builtins.__import__", guarded_import), patch.object(socket, "create_connection", side_effect=AssertionError("network")):
            validate_bundle(ROOT)
            preview_case(ROOT, "QC002")
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_input_rejects_answer_fields_and_naive_time(self):
        for key, value in [("l3_id", "L3-PK-001"), ("risk", "中"), ("expected", []), ("event_at", "2026-09-01T10:00:00")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.mutated("records", lambda d: d["records"][0].update({key: value}), load_records)

    def test_input_rejects_numeric_or_false_synthetic_marker(self):
        for value in [False, 1, "true"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.mutated("records", lambda d: d.update(synthetic=value), load_records)

    def test_duplicate_and_missing_reference_cases_rejected(self):
        for change in [lambda d: d["results"].append(deepcopy(d["results"][0])), lambda d: d["results"].pop()]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.mutated("references", change)

    def test_unknown_l3_and_false_risk_are_rejected(self):
        for key, value in [("l3_id", "L3-FAKE"), ("risk", "低"), ("suggested_decision", "需人工判定"), ("l4_confirmed", "made-up")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.mutated("references", lambda d: d["results"][0]["findings"][0].update({key: value}))

    def test_missing_information_and_high_risk_route_cannot_disappear(self):
        with self.assertRaises(ValueError):
            self.mutated("references", lambda d: d["results"][7].update(missing_facts=[]))
        with self.assertRaises(ValueError):
            self.mutated("references", lambda d: d["results"][2].update(triage_priority="routine"))

    def test_changed_quotes_and_wrong_arithmetic_rejected(self):
        with self.assertRaises(ValueError):
            self.mutated("references", lambda d: d["results"][0].update(record_quotes=["离心10:99"]))
        with self.assertRaises(ValueError):
            self.mutated("references", lambda d: d["results"][0]["arithmetic_checks"][0].update(expected_value=99))
        with self.assertRaises(ValueError):
            self.mutated("rules", lambda d: d["rules"][0]["source"].update(quote="不存在的条款"))

    def test_rule_parameters_required_facts_and_source_scope_checked(self):
        changes = [lambda d: d["rules"][0]["parameters"].update(minimum_minutes=True),
                   lambda d: d["rules"][0]["parameters"].update(minimum_minutes=-1),
                   lambda d: d["rules"][0]["parameters"].update(minimum_minutes=999),
                   lambda d: d["rules"][0].update(required_facts=["collected_at"]),
                   lambda d: d["rules"][0]["scope"].update(study_id="SYN-OTHER")]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.mutated("rules", change)

    def test_rule_invalid_version_link_rejected(self):
        with self.assertRaises(ValueError):
            self.mutated("rules", lambda d: d["rules"][6].update(supersedes=["SYN-RULE-MISSING@1"]))

    def test_normal_study_cannot_hide_new_conflict(self):
        def add_conflict(d):
            row = deepcopy(d["rules"][0])
            row["rule_id"] = "SYN-RULE-UNEXPECTED"
            row["risk"] = "高"
            d["rules"].append(row)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.mutated("rules", add_conflict)

    def test_input_scope_expired_or_wrong_site_rejected(self):
        for key, value in [("site_id", "SYN-SITE-OTHER"), ("event_at", "2026-10-01T00:00:00+08:00")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.mutated("records", lambda d: d["records"][0].update({key: value}))

    def test_negative_fractional_and_timezone_arithmetic(self):
        for start, end, expected in [("2026-09-01T23:50:00+08:00", "2026-09-02T00:20:00+08:00", Decimal(30)),
                                     ("2026-09-01T10:06:00+08:00", "2026-09-01T10:33:30+08:00", Decimal("27.5")),
                                     ("2026-09-01T02:06:00+00:00", "2026-09-01T10:33:00+08:00", Decimal(27))]:
            check = ArithmeticCheck(operation="elapsed_minutes", operands={"start": start, "end": end}, expected_value=27, note="test")
            self.assertEqual(arithmetic_value(check), expected)
        check = ArithmeticCheck(operation="elapsed_minutes", operands={"start": "2026-09-01T10:33:00+08:00", "end": "2026-09-01T10:06:00+08:00"}, expected_value=27, note="test")
        with self.assertRaises(ValueError):
            arithmetic_value(check)

    def test_inventory_does_not_accept_missing_or_negative_operands(self):
        for operands in [{"opening": 100}, {"opening": -1, "received": 1, "dispensed": 1, "returned_to_stock": 0}]:
            with self.subTest(operands=operands), self.assertRaises(ValueError):
                arithmetic_value(ArithmeticCheck(operation="inventory_closing", operands=operands, expected_value=0, note="test"))

    def test_json_duplicate_keys_rejected(self):
        with tempfile.TemporaryDirectory(prefix="clinical-qc-test-") as directory:
            root = Path(directory)
            file = root / FILES["records"]
            file.parent.mkdir(parents=True)
            file.write_text('{"schema_version":"m1-v1","synthetic":true,"synthetic":false,"records":[]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
                load_records(root)

    def test_preview_unknown_case_rejected(self):
        with self.assertRaises(ValueError):
            preview_case(ROOT, "not-a-case")


if __name__ == "__main__":
    unittest.main()
