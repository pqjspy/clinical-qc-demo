"""Loopback-only M3 HTTP layer; authority comes from server sessions, never UI roles."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import secrets
import threading
import time
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .contracts import CaseRecord, StrictModel
from .web_store import Store, Conflict
from .m4_workflow import analyze_record

ORIGINS = {"http://127.0.0.1:8910", "http://127.0.0.1:8911", "http://localhost:8910", "http://localhost:8911"}


class Login(StrictModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class JobRequest(StrictModel):
    case_id: str = Field(min_length=1, max_length=100)
    request_key: str = Field(min_length=16, max_length=100)


class HumanFinal(StrictModel):
    status: Literal["proposed_findings", "no_finding_for_checked_rule", "needs_information"]
    l3_id: str | None
    risk: Literal["低", "中", "高", "待定", "不适用"]
    suggested_decision: Literal["方案偏离", "需人工判定", "本项未命中", "待补充"]
    explanation: str = Field(min_length=1, max_length=4000)
    suggested_action: str = Field(min_length=1, max_length=2000)


class ReviewRequest(StrictModel):
    issue_id: str | None = Field(default=None, max_length=20)
    action: Literal["confirm", "edit", "return"]
    reason: str = Field(min_length=3, max_length=2000, pattern=r"\S.*\S")
    attested: Literal[True]
    expected_revision: int = Field(ge=0)
    request_key: str = Field(min_length=16, max_length=100)
    final: HumanFinal | None = None


class AlertRequest(StrictModel):
    state: Literal["seen", "transferred"]
    reason: str = Field(min_length=3, max_length=1000)


class DraftRequest(StrictModel):
    knowledge: dict
    base_id: str
    note: str = Field(min_length=3, max_length=1000)


def create_app(root=None, analyzer=None):
    root = Path(root or Path(__file__).resolve().parents[2])
    store = Store(root)
    execute = analyzer or analyze_record
    sessions, attempts = {}, []
    auth_lock = threading.Lock()
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qc-local-model")

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted()
        yield
        worker.shutdown(wait=True, cancel_futures=False)

    app = FastAPI(title="合成质控本地工作台", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"] if analyzer else ["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def safety(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("origin") not in ORIGINS:
                return JSONResponse({"detail": "拒绝跨站写入：请从本机工作台操作。"}, 403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "仅接受JSON。"}, 415)
            # Bound streamed bodies too; Content-Length is not trusted.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 700000:
                    return JSONResponse({"detail": "请求过大。"}, 413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)[:3000]}, 422)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, 404)

    def current(request: Request):
        token = request.cookies.get("qc_session", "")
        with auth_lock:
            session = sessions.get(token)
            if not session or session["until"] < time.monotonic():
                sessions.pop(token, None)
                raise HTTPException(401, "会话已失效，请登录。")
        if request.method != "GET" and not secrets.compare_digest(request.headers.get("x-qc-csrf", ""), session["csrf"]):
            raise HTTPException(403, "请刷新会话后再提交。")
        return session

    def reviewer(user=Depends(current)):
        if user["role"] != "reviewer":
            raise HTTPException(403, "只有复核者可以执行此操作。")
        return user

    def admin(user=Depends(current)):
        if user["role"] != "rule_admin":
            raise HTTPException(403, "只有规则管理员可以发布规则。")
        return user

    @app.get("/api/health")
    def health():
        return {"ok": True, "synthetic": True, "scope": "m4_six_families_explicit_grammar", "model_mode": "live_local" if analyzer is None else "test_double"}

    @app.post("/api/login")
    def login(payload: Login, response: Response, request: Request):
        with auth_lock:
            moment = time.monotonic()
            attempts[:] = [t for t in attempts if moment - t < 60]
            if len(attempts) >= 12:
                raise HTTPException(429, "登录请求过多，请一分钟后再试。")
            attempts.append(moment)
        user = store.authenticate(payload.username, payload.password)
        if not user:
            raise HTTPException(401, "用户名或密码错误。")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with auth_lock:
            sessions.pop(request.cookies.get("qc_session", ""), None)
            sessions[token] = {**user, "csrf": csrf, "until": time.monotonic() + 28800}
        response.set_cookie("qc_session", token, httponly=True, samesite="strict", max_age=28800, path="/api")
        with store.db(write=True) as db:
            store.audit(db, user["name"], "login", user["name"], {})
        return {**user, "csrf": csrf}

    @app.get("/api/me")
    def me(user=Depends(current)):
        return {k: user[k] for k in ("name", "role", "csrf")}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, user=Depends(current)):
        with auth_lock:
            sessions.pop(request.cookies.get("qc_session", ""), None)
        response.delete_cookie("qc_session", path="/api")
        return {"ok": True}

    @app.get("/api/cases")
    def cases(user=Depends(current)):
        return store.cases()

    @app.post("/api/cases", status_code=201)
    def add_case(payload: CaseRecord, user=Depends(reviewer)):
        store.add_case(payload, user["name"])
        return {"id": payload.case_id}

    def run_job(jid):
        try:
            record, bundle = store.job_input(jid)
            result = execute(root, record, knowledge_snapshot=bundle, progress=lambda stage: store.progress(jid, stage))
            store.finish_job(jid, result=result)
        except Exception as exc:
            store.finish_job(jid, error=f"{type(exc).__name__}: {str(exc)[:500]}")

    @app.post("/api/jobs", status_code=202)
    def start_job(payload: JobRequest, user=Depends(reviewer)):
        jid, created = store.create_job(payload.case_id, user["name"], payload.request_key)
        if created:
            worker.submit(run_job, jid)
        return {"id": jid, "created": created}

    @app.get("/api/jobs")
    def jobs(user=Depends(current)):
        return store.jobs()

    @app.get("/api/jobs/{jid}")
    def job(jid: str, user=Depends(current)):
        return store.detail(jid)

    @app.get("/api/jobs/{jid}/export")
    def export(jid: str, user=Depends(current)):
        return JSONResponse({"synthetic": True, "notice": "合成演示，非临床用途；应用层审计并非不可篡改存储。", **store.detail(jid)}, headers={"Content-Disposition": 'attachment; filename="synthetic-qc-run.json"'})

    @app.post("/api/jobs/{jid}/reviews", status_code=201)
    def review(jid: str, payload: ReviewRequest, user=Depends(reviewer)):
        return {"revision": store.review(jid, payload.model_dump(), user["name"])}

    @app.post("/api/jobs/{jid}/alerts")
    def alert(jid: str, payload: AlertRequest, user=Depends(reviewer)):
        store.set_alert(jid, payload.state, payload.reason, user["name"])
        return {"ok": True}

    @app.get("/api/library")
    def library(user=Depends(current)):
        return store.library()

    @app.get("/api/library/example")
    def example(user=Depends(admin)):
        base = store.active_bundle()
        data = copy.deepcopy(base["knowledge"])
        suffix = uuid4().hex[:6].upper()
        study, protocol, doc = f"SYN-STUDY-M3-{suffix}", f"SYN-PROTOCOL-M3-{suffix}-v1", f"SYN-DOC-M3-{suffix}-v1"
        rule = copy.deepcopy(data["rules"]["rules"][0])
        rule.update(rule_id=f"SYN-RULE-PK-M3-{suffix}", version="1", supersedes=[])
        rule["scope"].update(study_id=study, protocol_id=protocol)
        rule["parameters"]["minimum_minutes"] = 25
        quote = "【合成演示规则，非临床操作指引】本虚拟研究规定同一PK样本采血至开始离心至少25分钟；小于25分钟提示样本处理时间不足，建议风险为中，建议判定为方案偏离，须人工复核。"
        rule["source"] = {"document_id": doc, "section_id": "SEC-PK-001", "quote": quote}
        data["rules"]["rules"].append(rule)
        data["protocols"]["documents"].append({"document_id": doc, "study_id": study, "protocol_id": protocol, "version": "1", "effective_from": rule["scope"]["effective_from"], "effective_to": rule["scope"]["effective_to"], "sections": [{"section_id": "SEC-PK-001", "text": quote}]})
        return {"base_id": base["id"], "knowledge": data, "note": "合成M3演示：新研究PK阈值25分钟；不改原研究。"}

    @app.post("/api/drafts", status_code=201)
    def draft(payload: DraftRequest, user=Depends(admin)):
        return {"id": store.add_draft(payload.knowledge, payload.base_id, payload.note, user["name"])}

    @app.post("/api/drafts/{did}/publish")
    def publish(did: str, user=Depends(admin)):
        return {"bundle_id": store.publish(did, user["name"])}

    @app.get("/api/audit")
    def audit(user=Depends(current)):
        return store.audit_log()

    @app.get('/api/evaluation')
    def evaluation(user=Depends(current)):
        report = root / 'runtime/evaluations/m4-v1/report.json'
        if not report.exists():
            return {'available': False, 'notice': '18条封存合成评测尚未完成。这里不会启动模型或显示预期分数。'}
        return {'available': True, 'report': json.loads(report.read_text(encoding='utf-8'))}

    static = root / "frontend" / "dist"
    if static.exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    return app
