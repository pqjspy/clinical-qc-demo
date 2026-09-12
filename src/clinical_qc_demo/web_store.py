"""M3 local SQLite repository. Application-level append-only history, not regulated storage.

No reference reader is used here. Jobs may change; results/reviews/publications may not.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
from pathlib import Path
import secrets
import sqlite3
from uuid import uuid4

from .contracts import CaseRecord, Taxonomy, Protocols, Rules, aware_time
from .data import load_records, load_knowledge, validate_knowledge, rule_conflicts
from .workflow import canonical, digest


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        folder = self.root / "runtime" / "web"
        if not folder.resolve().is_relative_to(self.root):
            raise ValueError("Database path cannot escape project.")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = folder / "qc.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Database symlink not allowed.")
        self.credentials = folder / "local_accounts.json"
        self.initialize()

    @contextmanager
    def db(self, write=False):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, role TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bundles(id TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), bundle_id TEXT NOT NULL REFERENCES bundles(id), state TEXT NOT NULL, stage TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS runs(job_id TEXT PRIMARY KEY REFERENCES jobs(id), run_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES runs(job_id), revision INTEGER NOT NULL, payload TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL, UNIQUE(job_id,revision));
                CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, base_id TEXT NOT NULL REFERENCES bundles(id), payload TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS publications(draft_id TEXT PRIMARY KEY REFERENCES drafts(id), bundle_id TEXT NOT NULL REFERENCES bundles(id), actor TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_jobs_case_created ON jobs(case_id,created_at);
                CREATE INDEX IF NOT EXISTS idx_alerts_job_id ON alerts(job_id,id);
                CREATE INDEX IF NOT EXISTS idx_audit_target ON audit(target,id);
            ''')
            for table in ("cases", "bundles", "runs", "reviews", "drafts", "publications", "audit", "alerts"):
                for verb in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END")
            db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", (now(),))
            db.execute("PRAGMA optimize")
        self.path.chmod(0o600)
        with self.db(write=True) as db:
            if not db.execute("SELECT 1 FROM users").fetchone():
                accounts = {}
                for name, role in (("viewer", "viewer"), ("reviewer", "reviewer"), ("rule_admin", "rule_admin")):
                    password, salt = secrets.token_urlsafe(15), secrets.token_hex(16)
                    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200000).hex()
                    db.execute("INSERT INTO users VALUES(?,?,?,?)", (name, role, salt, hashed))
                    accounts[name] = password
                # Deliberately exclusive: never silently replace previously issued local credentials.
                with self.credentials.open("x", encoding="utf-8") as handle:
                    self.credentials.chmod(0o600)
                    json.dump(accounts, handle, indent=2)
            if not db.execute("SELECT 1 FROM bundles").fetchone():
                t, p, r = load_knowledge(self.root)
                bundle = dict(taxonomy=t.model_dump(), protocols=p.model_dump(), rules=r.model_dump())
                db.execute("INSERT INTO bundles VALUES(?,?,?,?,?)", ("m1-seed", canonical(bundle), digest(bundle), now(), "system"))
                db.execute("INSERT INTO settings VALUES('active_bundle','m1-seed')")
            if not db.execute("SELECT 1 FROM cases").fetchone():
                for record in load_records(self.root):
                    db.execute("INSERT INTO cases VALUES(?,?,?,?)", (record.case_id, canonical(record.model_dump()), now(), "system"))
                for name in ("m2_pk_40_minutes.json", "m2_pk_v2.json"):
                    data = json.loads((self.root / "examples" / name).read_text())
                    record = CaseRecord.model_validate(data)
                    db.execute("INSERT OR IGNORE INTO cases VALUES(?,?,?,?)", (record.case_id, canonical(data), now(), "system"))

    @staticmethod
    def audit(db, actor, action, target, details):
        db.execute("INSERT INTO audit(actor,action,target,details,created_at) VALUES(?,?,?,?,?)", (actor, action, target, canonical(details), now()))

    def authenticate(self, name, password):
        with self.db() as db:
            row = db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        salt = bytes.fromhex(row["salt"]) if row else b"invalid-user-salt"
        hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000).hex()
        if row and secrets.compare_digest(hashed, row["password_hash"]):
            return {"name": row["name"], "role": row["role"]}
        return None

    def active_bundle(self, db=None):
        if db is None:
            with self.db() as con:
                return self.active_bundle(con)
        row = db.execute("SELECT * FROM bundles WHERE id=(SELECT value FROM settings WHERE key='active_bundle')").fetchone()
        return {"id": row["id"], "sha256": row["sha256"], "knowledge": json.loads(row["payload"])}

    def cases(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM cases ORDER BY created_at,id")]

    def add_case(self, record, actor):
        if not record.text.startswith("【合成虚拟记录】") or len(record.text) > 8000 or not all(x.startswith("SYN-") for x in (record.study_id, record.site_id, record.protocol_id)):
            raise ValueError("只接受标明【合成虚拟记录】的输入及 SYN- 虚拟编号；最多8000字。")
        if len(record.case_id) > 100:
            raise ValueError("记录编号过长。")
        with self.db(write=True) as db:
            if db.execute("SELECT 1 FROM cases WHERE id=?", (record.case_id,)).fetchone():
                raise Conflict("记录编号已存在；原文不可覆盖，请另存新编号。")
            db.execute("INSERT INTO cases VALUES(?,?,?,?)", (record.case_id, canonical(record.model_dump()), now(), actor))
            self.audit(db, actor, "case_created", record.case_id, {"input_sha256": digest(record.model_dump())})

    def create_job(self, case_id, actor, request_key):
        with self.db(write=True) as db:
            previous = db.execute("SELECT * FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if previous:
                if previous["case_id"] != case_id or previous["actor"] != actor:
                    raise Conflict("重复请求编号对应不同操作。")
                return previous["id"], False
            row = db.execute("SELECT payload FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row:
                raise KeyError("记录不存在。")
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running')").fetchone():
                raise Conflict("已有一条分析在运行；请完成后再提交，避免本机模型拥塞。")
            bundle = self.active_bundle(db)
            jid = str(uuid4())
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (jid, case_id, bundle["id"], "queued", "等待本机模型", actor, request_key, now(), None))
            self.audit(db, actor, "analysis_requested", jid, {"case_id": case_id, "bundle_id": bundle["id"]})
            return jid, True

    def job_input(self, jid):
        with self.db() as db:
            row = db.execute("SELECT c.payload AS record,b.payload AS bundle FROM jobs j JOIN cases c ON c.id=j.case_id JOIN bundles b ON b.id=j.bundle_id WHERE j.id=?", (jid,)).fetchone()
            return CaseRecord.model_validate_json(row["record"]), json.loads(row["bundle"])

    def progress(self, jid, stage):
        with self.db(write=True) as db:
            db.execute("UPDATE jobs SET state='running',stage=? WHERE id=? AND state IN ('running','queued')", (stage, jid))

    def finish_job(self, jid, result=None, error=None):
        with self.db(write=True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not job or job["state"] not in ("running", "queued"):
                raise Conflict("运行已经结束。")
            failed = error is not None or (result and result["status"] == "analysis_failed")
            if result:
                db.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (jid, result["run_id"], canonical(result), digest(result), now()))
            db.execute("UPDATE jobs SET state=?,stage=?,error=? WHERE id=?", ("failed" if failed else "completed", "分析失败，待处理" if failed else "等待人工复核", error, jid))
            db.execute("INSERT INTO alerts(job_id,state,actor,reason,created_at) VALUES(?,?,?,?,?)", (jid, "pending", "system", "新运行需人工复核", now()))
            self.audit(db, "system", "analysis_finished", jid, {"failed": bool(failed), "run_id": result.get("run_id") if result else None})

    def recover_interrupted(self):
        with self.db(write=True) as db:
            for row in db.execute("SELECT id FROM jobs WHERE state IN ('running','queued')").fetchall():
                db.execute("UPDATE jobs SET state='failed',stage='服务重启中断',error='服务重启；未自动重跑。请检查后显式重新分析。' WHERE id=?", (row[0],))
                db.execute("INSERT INTO alerts(job_id,state,actor,reason,created_at) VALUES(?,?,?,?,?)", (row[0], "pending", "system", "运行中断", now()))
                self.audit(db, "system", "analysis_interrupted", row[0], {})

    def jobs(self):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT j.id,j.case_id,j.bundle_id,j.state,j.stage,j.actor,j.created_at,j.error,r.run_id FROM jobs j LEFT JOIN runs r ON r.job_id=j.id ORDER BY j.created_at DESC")]
            for row in rows:
                review = db.execute("SELECT payload,revision FROM reviews WHERE job_id=? ORDER BY revision DESC LIMIT 1", (row["id"],)).fetchone()
                alert = db.execute("SELECT state FROM alerts WHERE job_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
                row["review_action"] = json.loads(review["payload"])["action"] if review else None
                row["review_revision"] = review["revision"] if review else 0
                row["alert_state"] = alert[0] if alert else None
                result = db.execute("SELECT payload FROM runs WHERE job_id=?", (row["id"],)).fetchone()
                data = json.loads(result[0]) if result else {}
                row["risk"] = data.get("risk", "待定")
                row["priority"] = data.get("triage_priority", "priority")
                row["result_status"] = data.get("status")
            return rows

    def detail(self, jid):
        with self.db() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not job:
                raise KeyError("运行不存在。")
            result = db.execute("SELECT * FROM runs WHERE job_id=?", (jid,)).fetchone()
            reviews = [dict(r) for r in db.execute("SELECT revision,payload,actor,created_at FROM reviews WHERE job_id=? ORDER BY revision", (jid,))]
            for item in reviews:
                item["payload"] = json.loads(item["payload"])
            return {"job": {k: job[k] for k in ("id", "case_id", "bundle_id", "state", "stage", "error", "created_at")},
                    "result": json.loads(result["payload"]) if result else None, "result_sha256": result["sha256"] if result else None,
                    "reviews": reviews, "alerts": [dict(r) for r in db.execute("SELECT state,actor,reason,created_at FROM alerts WHERE job_id=? ORDER BY id", (jid,))]}

    def review(self, jid, payload, actor):
        with self.db(write=True) as db:
            previous = db.execute("SELECT * FROM reviews WHERE request_key=?", (payload["request_key"],)).fetchone()
            request_hash = digest(payload)
            if previous:
                if previous["job_id"] != jid or previous["actor"] != actor or previous["request_hash"] != request_hash:
                    raise Conflict("重复请求内容不同。")
                return previous["revision"]
            row = db.execute("SELECT payload FROM runs WHERE job_id=?", (jid,)).fetchone()
            if not row:
                raise ValueError("没有可复核的完整运行记录；请先处理运行错误。")
            result = json.loads(row[0])
            original_result = result
            issue_id = payload.get('issue_id')
            is_m4 = result.get('schema_version') == 'm4-v1'
            if is_m4 and result.get('issues'):
                targets = [i for i in result['issues'] if i['issue_id'] == issue_id]
                if len(targets) != 1:
                    raise ValueError('M4必须逐问题项复核，不能用一个L3覆盖整条记录。')
                result = {**targets[0], 'knowledge_snapshot': result.get('knowledge_snapshot')}
                if original_result['status'] == 'analysis_failed' and payload['action'] == 'confirm':
                    raise ValueError('失败运行的诊断不能一键确认为成功结果。')
            elif issue_id is not None:
                raise ValueError('问题项不存在。')
            elif is_m4 and payload['action'] != 'return':
                raise ValueError('没有经过校验的问题项，只能退回排查，不能用一个人工结论关闭分析失败。')
            latest = db.execute("SELECT COALESCE(MAX(revision),0) FROM reviews WHERE job_id=?", (jid,)).fetchone()[0]
            if latest != payload["expected_revision"]:
                raise Conflict("其他操作已更新这次复核。请重新打开历史后再提交。")
            action = payload["action"]
            final = payload.get("final")
            resolved = result["status"] in ("proposed_findings", "no_finding_for_checked_rule")
            if action == "confirm":
                if not resolved or final is not None:
                    raise ValueError("失败/未决运行不能一键确认；只能退回，或明确编辑人工意见。")
                if is_m4 and original_result.get('explanation_error') and not result.get('explanation_draft'):
                    raise ValueError('本项解释未完成，请明确编辑人工意见或退回。')
                final = {"status": result["status"], "findings": result["findings"], "risk": result["risk"], "explanation": result.get("explanation_draft", {}).get("text", ""), "origin": "human_confirmed_original"}
            elif action == "edit":
                if final is None:
                    raise ValueError("请填写人工意见。")
                if not result.get('knowledge_snapshot'):
                    raise ValueError('缺少已核验分类快照，只能退回排查。')
                paths = {p["l3_id"]: p for p in result["knowledge_snapshot"]["taxonomy"]["paths"]}
                l3 = final["l3_id"]
                if l3 and l3 not in paths:
                    raise ValueError("分类不在本次运行绑定的分类树中。")
                if (final["status"] == "proposed_findings") != bool(l3):
                    raise ValueError("有问题才选择L3；未命中/待定不填分类。")
                if final["status"] == "proposed_findings" and final["risk"] not in ("低", "中", "高"):
                    raise ValueError("问题需给出明确的人工风险等级。")
                if final["status"] == "needs_information" and final["risk"] != "待定":
                    raise ValueError("待补充信息的风险应为待定。")
                if final["status"] == "no_finding_for_checked_rule" and final["risk"] != "不适用":
                    raise ValueError("本项未命中不能同时标问题风险。")
                allowed_decisions = {"proposed_findings": {"方案偏离", "需人工判定"},
                                     "no_finding_for_checked_rule": {"本项未命中"}, "needs_information": {"待补充"}}
                if final["suggested_decision"] not in allowed_decisions[final["status"]]:
                    raise ValueError("人工判定与处理状态矛盾，请核对。")
                final = {**final, "path": paths.get(l3), "origin": "human_edited_not_ai_prediction"}
            elif final is not None:
                raise ValueError("退回补充不附带已确认结论。")
            stored = {"action": action, "reason": payload["reason"], "final": final, "scope": "synthetic_m4_issue" if is_m4 else "synthetic_single_pk_demo", "attested": payload["attested"]}
            if is_m4:
                stored['issue_id'] = issue_id
            revision = latest + 1
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)", (str(uuid4()), jid, revision, canonical(stored), actor, now(), payload["request_key"], request_hash))
            # IDs/timestamps are server-derived. Audit and review are one transaction.
            self.audit(db, actor, "review_" + action, jid, {"revision": revision, "reason": payload["reason"], "issue_id": issue_id, "initial_result_sha256": digest(original_result)})
            alert = "transferred" if action == "return" or (final and final.get("status") == "needs_information") else "closed"
            if is_m4 and original_result.get('issues'):
                latest_by_issue = {}
                for item in db.execute('SELECT payload FROM reviews WHERE job_id=? ORDER BY revision', (jid,)):
                    opinion = json.loads(item[0])
                    latest_by_issue[opinion.get('issue_id')] = opinion
                settled = lambda opinion: bool(opinion and opinion['action'] != 'return' and opinion['final'] and opinion['final']['status'] in ('proposed_findings', 'no_finding_for_checked_rule'))
                all_settled = original_result['status'] != 'analysis_failed' and all(settled(latest_by_issue.get(i['issue_id'])) for i in original_result['issues'])
                alert = 'closed' if all_settled else ('transferred' if any(o['action'] == 'return' or o.get('final', {}).get('status') == 'needs_information' for o in latest_by_issue.values() if o.get('final') or o['action'] == 'return') else 'pending')
            db.execute("INSERT INTO alerts(job_id,state,actor,reason,created_at) VALUES(?,?,?,?,?)", (jid, alert, actor, payload["reason"], now()))
            return revision

    def set_alert(self, jid, state, reason, actor):
        with self.db(write=True) as db:
            if not db.execute("SELECT 1 FROM jobs WHERE id=? AND state IN ('completed','failed')", (jid,)).fetchone():
                raise ValueError("运行尚未结束或不存在。")
            db.execute("INSERT INTO alerts(job_id,state,actor,reason,created_at) VALUES(?,?,?,?,?)", (jid, state, actor, reason, now()))
            self.audit(db, actor, "alert_" + state, jid, {"reason": reason})

    def validate_addition(self, bundle, base):
        if set(bundle) != {"taxonomy", "protocols", "rules"}:
            raise ValueError("导入需含 taxonomy、protocols、rules 三部分。")
        t, p, r = validate_knowledge(Taxonomy.model_validate(bundle["taxonomy"]), Protocols.model_validate(bundle["protocols"]), Rules.model_validate(bundle["rules"]))
        if canonical(bundle["taxonomy"]) != canonical(base["taxonomy"]):
            raise ValueError("M3不修改分类树；留待M4。")
        for key, field, idfn in (("protocols", "documents", lambda x: x["document_id"]), ("rules", "rules", lambda x: x["rule_id"] + "@" + x["version"])):
            current = {idfn(x): x for x in bundle[key][field]}
            for old in base[key][field]:
                if current.get(idfn(old)) != old:
                    raise ValueError("已发布版本不可修改或删除；请添加新版本和新来源文档。")
        old_keys = {x["rule_id"] + "@" + x["version"] for x in base["rules"]["rules"]}
        added = [x for x in r.rules if x.key not in old_keys]
        if not added or any(x.check_type != "elapsed_minimum" for x in added):
            raise ValueError("M3只发布新增的PK规则版本。")
        for x in added:
            if not all(v.startswith("SYN-") for v in (x.rule_id, x.scope.study_id, x.scope.site_id, x.scope.protocol_id, x.source.document_id)) or "【合成" not in x.source.quote:
                raise ValueError("新增规则必须是显式合成规则。")
        previous = Rules.model_validate(base["rules"])
        new_conflicts = set(map(tuple, rule_conflicts(r.rules))) - set(map(tuple, rule_conflicts(previous.rules)))
        if new_conflicts:
            raise ValueError("新增版本产生冲突，不能发布。")
        # M2 requires a unique applicable PK rule, even when two policies agree.
        for a, b in combinations([x for x in r.rules if x.check_type == "elapsed_minimum"], 2):
            if a.key in old_keys and b.key in old_keys:
                continue
            same = (a.scope.study_id, a.scope.site_id, a.scope.protocol_id) == (b.scope.study_id, b.scope.site_id, b.scope.protocol_id)
            overlap = ((a.scope.effective_to is None or aware_time(b.scope.effective_from) < aware_time(a.scope.effective_to))
                       and (b.scope.effective_to is None or aware_time(a.scope.effective_from) < aware_time(b.scope.effective_to)))
            if same and overlap:
                raise ValueError("M3每个适用时点只允许一条PK规则；不能新增重叠规则。")
        return [x.key for x in added]

    def add_draft(self, bundle, base_id, note, actor):
        with self.db(write=True) as db:
            base = self.active_bundle(db)
            if base_id != base["id"]:
                raise Conflict("规则库已更新，请重新读取后导入。")
            added = self.validate_addition(bundle, base["knowledge"])
            did = str(uuid4())
            db.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?)", (did, base_id, canonical(bundle), actor, now(), note))
            self.audit(db, actor, "rule_draft_created", did, {"added": added, "note": note})
            return did

    def publish(self, did, actor):
        with self.db(write=True) as db:
            prior = db.execute("SELECT bundle_id FROM publications WHERE draft_id=?", (did,)).fetchone()
            if prior:
                return prior[0]
            row = db.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
            if not row:
                raise KeyError("草稿不存在。")
            base = self.active_bundle(db)
            if row["base_id"] != base["id"]:
                raise Conflict("草稿基于旧规则库；请重新校验并另存草稿。")
            payload = json.loads(row["payload"])
            added = self.validate_addition(payload, base["knowledge"])
            bid = str(uuid4())
            db.execute("INSERT INTO bundles VALUES(?,?,?,?,?)", (bid, canonical(payload), digest(payload), now(), actor))
            db.execute("INSERT INTO publications VALUES(?,?,?,?)", (did, bid, actor, now()))
            db.execute("UPDATE settings SET value=? WHERE key='active_bundle'", (bid,))
            self.audit(db, actor, "rule_bundle_published", bid, {"draft_id": did, "added": added})
            return bid

    def library(self):
        with self.db() as db:
            drafts = [dict(r) for r in db.execute("SELECT d.*,p.bundle_id AS published_bundle FROM drafts d LEFT JOIN publications p ON p.draft_id=d.id ORDER BY d.created_at DESC")]
            for d in drafts:
                d["knowledge"] = json.loads(d.pop("payload"))
            return {**self.active_bundle(db), "drafts": drafts}

    def audit_log(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 300")]
