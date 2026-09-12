"""M3 HTTP/storage regression tests. Model calls are explicitly TEST DOUBLES."""
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from clinical_qc_demo.web_api import create_app
from clinical_qc_demo.web_store import Store, Conflict
from clinical_qc_demo.workflow import analyze_record
from test_m2_workflow import FakeClient

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://127.0.0.1:8911"}


def fake_analyze(root, record, **kw):
    return analyze_record(root, record, client=FakeClient(), **kw)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for folder in ("data/knowledge", "data/inputs", "examples"):
            shutil.copytree(ROOT / folder, self.root / folder)
        # No reference directory exists: a successful flow cannot have read it.
        self.app = create_app(self.root, analyzer=fake_analyze)
        self.store = self.app.state.store
        self.c = TestClient(self.app, base_url="http://127.0.0.1:8911")
        self.c.__enter__()
        self.accounts = json.loads(self.store.credentials.read_text())
        self.login()

    def tearDown(self):
        self.c.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, name="reviewer"):
        r = self.c.post("/api/login", json={"username": name, "password": self.accounts[name]}, headers=ORIGIN)
        self.assertEqual(r.status_code, 200, r.text)
        self.headers = {**ORIGIN, "X-QC-CSRF": r.json()["csrf"]}

    def post(self, path, payload):
        return self.c.post(path, json=payload, headers=self.headers)

    def run_case(self):
        r = self.post("/api/jobs", {"case_id": "QC002", "request_key": str(uuid4())})
        self.assertEqual(r.status_code, 202, r.text)
        jid = r.json()["id"]
        for _ in range(100):
            d = self.c.get("/api/jobs/" + jid).json()
            if d["job"]["state"] not in ("running", "queued"):
                return jid, d
            time.sleep(.01)
        self.fail("Test-double worker did not finish")

    def review_payload(self, **changes):
        payload = {"action": "confirm", "reason": "离线合成测试确认，不是临床审核", "attested": True,
                   "expected_revision": 0, "request_key": str(uuid4()), "final": None}
        payload.update(changes)
        return payload

    def test_live_path_is_test_double_and_input_not_reference(self):
        jid, d = self.run_case()
        self.assertEqual(d["result"]["status"], "proposed_findings")
        self.assertEqual(d["result"]["mode"], "test_double")
        self.assertEqual(d["result"]["calculation"]["actual_minutes"], "27")
        self.assertEqual(d["job"]["bundle_id"], "m1-seed")
        self.assertFalse(d["result"]["reference_answers_read"])

    def test_m4_evaluation_is_authenticated_read_only_saved_report(self):
        self.assertFalse(self.c.get('/api/evaluation').json()['available'])
        out=self.root/'runtime/evaluations/m4-v1'
        out.mkdir(parents=True)
        report={'synthetic':True,'planned':18,'test_double_report':True}
        (out/'report.json').write_text(json.dumps(report))
        jobs_before=self.c.get('/api/jobs').json()
        self.assertEqual(self.c.get('/api/evaluation').json(),{'available':True,'report':report})
        self.assertEqual(self.c.get('/api/jobs').json(),jobs_before)
        self.c.cookies.clear()
        self.assertEqual(self.c.get('/api/evaluation').status_code,401)

    def test_review_restart_and_initial_hash_preserved(self):
        jid, d = self.run_case()
        r = self.post(f"/api/jobs/{jid}/reviews", self.review_payload())
        self.assertEqual(r.status_code, 201, r.text)
        reopened = Store(self.root).detail(jid)
        self.assertEqual(reopened["result"], d["result"])
        self.assertEqual(reopened["result_sha256"], d["result_sha256"])
        self.assertEqual(reopened["reviews"][0]["actor"], "reviewer")
        self.assertEqual(reopened["alerts"][-1]["state"], "closed")
        second = self.post(f"/api/jobs/{jid}/reviews", self.review_payload(action="return", expected_revision=1))
        self.assertEqual(second.json()["revision"], 2)
        self.assertEqual(self.store.detail(jid)["alerts"][-1]["state"], "transferred")

    def test_duplicate_and_stale_review(self):
        jid, _ = self.run_case()
        p = self.review_payload()
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", p).status_code, 201)
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", p).json()["revision"], 1)
        self.assertEqual(len(self.store.detail(jid)["reviews"]), 1)
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", self.review_payload()).status_code, 409)
        p["reason"] = "相同编号不同内容"
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", p).status_code, 409)

    def test_permission_matrix_and_forged_actor(self):
        jid, _ = self.run_case()
        self.login("viewer")
        self.assertEqual(self.c.get("/api/jobs/" + jid).status_code, 200)
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", self.review_payload()).status_code, 403)
        self.assertEqual(self.post("/api/jobs", {"case_id":"QC002","request_key":str(uuid4())}).status_code, 403)
        self.login("rule_admin")
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", self.review_payload()).status_code, 403)
        self.login("reviewer")
        self.assertEqual(self.post("/api/drafts/missing/publish", {}).status_code, 403)
        p = self.review_payload(actor="rule_admin")
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", p).status_code, 422)

    def test_csrf_origin_and_login_required(self):
        self.assertEqual(self.c.post("/api/jobs", json={}, headers=ORIGIN).status_code, 403)
        self.assertEqual(self.c.post("/api/logout", json={}, headers={**self.headers,"Origin":"https://evil.example"}).status_code,403)
        self.post("/api/logout", {})
        self.assertEqual(self.c.get("/api/cases").status_code, 401)
        self.assertEqual(self.c.get("/api/cases", headers={"Host":"evil.example"}).status_code, 400)

    def test_invalid_review_no_attestation_no_reason(self):
        jid, _ = self.run_case()
        for p in (self.review_payload(attested=False),self.review_payload(reason=""),self.review_payload(reason="   ")):
            self.assertEqual(self.post(f"/api/jobs/{jid}/reviews",p).status_code,422)

    def test_illegal_l3_and_inconsistent_human_status(self):
        jid, _ = self.run_case()
        final={"status":"no_finding_for_checked_rule","l3_id":None,"risk":"不适用","suggested_decision":"方案偏离","explanation":"测试人工意见","suggested_action":"仅演示"}
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", self.review_payload(action="edit",final=final)).status_code,422)
        final.update(status="proposed_findings",risk="中",l3_id="INVENTED")
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews", self.review_payload(action="edit",final=final)).status_code,422)
        final["l3_id"]=self.store.detail(jid)["result"]["findings"][0]["l3_id"]
        r=self.post(f"/api/jobs/{jid}/reviews", self.review_payload(action="edit",final=final))
        self.assertEqual(r.status_code,201,r.text)
        self.assertEqual(self.store.detail(jid)["reviews"][0]["payload"]["final"]["path"]["l1"],"样本管理")

    def test_audit_failure_rolls_back_review(self):
        jid, _ = self.run_case()
        with patch.object(self.store,"audit",side_effect=RuntimeError("injected audit failure")):
            with self.assertRaises(RuntimeError):
                self.store.review(jid,self.review_payload(),"reviewer")
        self.assertEqual(self.store.detail(jid)["reviews"],[])
        self.assertEqual(self.store.detail(jid)["alerts"][-1]["state"],"pending")

    def test_database_tables_reject_overwrite(self):
        jid, _ = self.run_case()
        for sql in ("UPDATE runs SET sha256='bad'", "DELETE FROM cases", "UPDATE bundles SET payload='{}'"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.db(write=True) as db: db.execute(sql)

    def test_rule_draft_publish_snapshot_old_result_preserved(self):
        jid, d = self.run_case()
        self.login("rule_admin")
        p=self.c.get("/api/library/example").json()
        r=self.post("/api/drafts",p)
        self.assertEqual(r.status_code,201,r.text)
        self.assertEqual(self.store.active_bundle()["id"],"m1-seed")
        did=r.json()["id"]
        pub=self.post(f"/api/drafts/{did}/publish",{})
        self.assertEqual(pub.status_code,200,pub.text)
        self.assertNotEqual(pub.json()["bundle_id"],"m1-seed")
        self.assertEqual(self.store.detail(jid)["result_sha256"],d["result_sha256"])
        self.assertEqual(len(self.store.active_bundle()["knowledge"]["rules"]["rules"]),10)
        # Submission fixes bundle once; changing active bundle later cannot mutate it.
        j2,_=self.store.create_job("QC002","reviewer",str(uuid4()))
        self.assertEqual(self.store.job_input(j2)[1],p["knowledge"])
        self.store.finish_job(j2,error="test-only stop")
        self.assertEqual(self.post(f"/api/drafts/{did}/publish",{}).json(),pub.json())

    def test_rule_old_version_mutation_and_equivalent_overlap_rejected(self):
        self.login("rule_admin")
        p=self.c.get("/api/library/example").json()
        p["knowledge"]["rules"]["rules"][0]["risk"]="高"
        self.assertEqual(self.post("/api/drafts",p).status_code,422)
        p=self.c.get("/api/library/example").json()
        duplicate=copy.deepcopy(p["knowledge"]["rules"]["rules"][0]);duplicate["rule_id"]="SYN-EQUIVALENT-DUPLICATE"
        p["knowledge"]["rules"]["rules"].append(duplicate)
        self.assertEqual(self.post("/api/drafts",p).status_code,422)

    def test_publish_rolls_back_and_stale_draft_rejected(self):
        self.login("rule_admin")
        p=self.c.get("/api/library/example").json()
        did=self.post("/api/drafts",p).json()["id"]
        second=self.post("/api/drafts",p).json()["id"]
        with patch.object(self.store,"audit",side_effect=RuntimeError("injected publish failure")):
            with self.assertRaises(RuntimeError):self.store.publish(did,"rule_admin")
        self.assertEqual(self.store.active_bundle()["id"],"m1-seed")
        self.assertEqual(self.post(f"/api/drafts/{did}/publish",{}).status_code,200)
        self.assertEqual(self.post(f"/api/drafts/{second}/publish",{}).status_code,409)

    def test_interrupted_job_not_auto_rerun_or_success(self):
        jid,created=self.store.create_job("QC002","reviewer",str(uuid4()))
        with self.assertRaises(Conflict):self.store.create_job("QC002","reviewer",str(uuid4()))
        self.store.recover_interrupted()
        self.assertEqual(self.store.detail(jid)["job"]["state"],"failed")
        self.assertIsNone(self.store.detail(jid)["result"])

    def test_job_retry_returns_same_run_and_failure_cannot_confirm(self):
        key=str(uuid4())
        jid,created=self.store.create_job("QC002","reviewer",key)
        again,created_again=self.store.create_job("QC002","reviewer",key)
        self.assertEqual((again,created_again),(jid,False))
        record,bundle=self.store.job_input(jid)
        failed=analyze_record(self.root,record,client=FakeClient(failure="preflight"),knowledge_snapshot=bundle)
        self.store.finish_job(jid,result=failed)
        self.assertEqual(self.store.detail(jid)["job"]["state"],"failed")
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews",self.review_payload()).status_code,422)
        self.assertEqual(self.post(f"/api/jobs/{jid}/reviews",self.review_payload(action="return")).status_code,201)

    def test_input_edit_is_new_record_and_original_preserved(self):
        original=self.store.cases()[0]
        self.assertEqual(self.post("/api/cases",original).status_code,409)
        copy_record={**original,"case_id":"SYN-NEW-INPUT","text":original["text"]+" 请核实。"}
        self.assertEqual(self.post("/api/cases",copy_record).status_code,201)
        self.assertEqual(self.store.cases()[0],original)
        copy_record["case_id"]="SYN-INVALID";copy_record["text"]="未标明合成"
        self.assertEqual(self.post("/api/cases",copy_record).status_code,422)


if __name__ == "__main__":unittest.main()
