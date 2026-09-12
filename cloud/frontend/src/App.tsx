import { useState, useEffect, useRef } from "react";
type Data = Record<string, any>;
type RecordInput = {
  case_id: string;
  study_id: string;
  site_id: string;
  protocol_id: string;
  event_at: string;
  timezone: string;
  text: string;
};
const labels: Record<string, string> = {
  proposed_findings: "发现问题 · 待复核",
  no_finding_for_checked_rule: "本项未命中 · 待复核",
  analysis_failed: "分析失败",
  partial_review_required: "部分完成 · 仍需逐项处理",
  rule_not_found: "未找到适用规则",
  unsupported_scope: "超出当前支持范围",
  out_of_scope: "超出范围",
  needs_information: "需补充信息",
  ambiguous_evidence: "事实有歧义",
  rule_conflict: "规则冲突",
  queued: "等待中",
  running: "分析中",
  completed: "已完成",
  failed: "失败",
  confirm: "确认原初判",
  edit: "人工修改",
  return: "退回补充",
  pending: "待处理",
  seen: "已查看",
  transferred: "已转交",
  closed: "已关闭",
  reviewer: "复核者",
  viewer: "查看者",
  rule_admin: "规则管理员",
};
const stages: Record<string, string> = {
  load_validated_knowledge: "核验规则快照",
  local_model_preflight: "检查云端模型",
  qwen_extract_verbatim_evidence: "Qwen 正在抽取原文事实",
  validate_evidence_and_normalize: "核对原文与时间",
  scope_filter_and_bm25: "检索适用规则",
  python_pk_calculation: "Python 计算时间间隔",
  qwen_organize_explanation: "Qwen 正在生成解释草稿",
  validate_model_result_selection: "检查结果与引用编号",
  verify_model_identity_unchanged: "核验模型版本",
  route_unresolved_to_review: "转交未决问题",
  qwen_decompose_issues: "Qwen 正在拆分问题与识别家族",
  verify_complete_coverage: "检查是否遗漏原文片段",
};
const display = (s: string) => labels[s] || s;
const time = (s: string) =>
  s ? new Date(s).toLocaleString("zh-CN", { hour12: false }) : "";
const uid = () => crypto.randomUUID();
function Json({ data }: { data: unknown }) {
  return <pre>{JSON.stringify(data, null, 2)}</pre>;
}
export default function App() {
  const [user, setUser] = useState<Data | null>(null),
    [authReady, setAuthReady] = useState(false),
    [csrf, setCsrf] = useState("");
  const [page, setPage] = useState("records"),
    [cases, setCases] = useState<RecordInput[]>([]),
    [form, setForm] = useState<RecordInput | null>(null),
    [jobs, setJobs] = useState<Data[]>([]),
    [jobId, setJobId] = useState(""),
    [detail, setDetail] = useState<Data | null>(null),
    [library, setLibrary] = useState<Data | null>(null),
    [audit, setAudit] = useState<Data[]>([]);
  const [evaluation, setEvaluation] = useState<Data | null>(null);
  const [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [working, setWorking] = useState(false),
    [username, setUsername] = useState("reviewer"),
    [password, setPassword] = useState("");
  const [quote, setQuote] = useState(""),
    [quoteAt, setQuoteAt] = useState<number | null>(null),
    [issueId, setIssueId] = useState(""),
    [reviewAction, setReviewAction] = useState("edit"),
    [reason, setReason] = useState(""),
    [attested, setAttested] = useState(false),
    [human, setHuman] = useState<Data>({});
  const [draftText, setDraftText] = useState(""),
    [draftNote, setDraftNote] = useState(
      "合成规则草稿，请人工核验参数、来源和适用范围。",
    );
  const evidenceRef = useRef<HTMLDivElement>(null);
  const selectedRun = useRef("");
  const writable = user?.role === "reviewer",
    admin = user?.role === "rule_admin";
  const result = detail?.result,
    busy = jobs.some((j) => ["running", "queued"].includes(j.state));
  const reviewTarget =
    result?.issues?.find((i: Data) => i.issue_id === issueId) ||
    result?.issues?.[0] ||
    result;
  const currentIssueId = reviewTarget?.issue_id || null;
  const resolved =
    reviewTarget &&
    result?.status !== "analysis_failed" &&
    ["proposed_findings", "no_finding_for_checked_rule"].includes(
      reviewTarget.status,
    ) &&
    (!result?.explanation_error || !!reviewTarget.explanation_draft);
  async function api(path: string, body?: unknown) {
    const response = await fetch("/api" + path, {
      method: body === undefined ? "GET" : "POST",
      headers:
        body === undefined
          ? {}
          : { "Content-Type": "application/json", "X-QC-CSRF": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(()=>({detail:'云端连接中断，请保留输入并稍后刷新。没有自动重试。'}));
    if (!response.ok) {
      if (response.status === 401) {
        setUser(null); setCsrf(''); setCases([]); setJobs([]); setDetail(null); setForm(null);
        setAudit([]); setLibrary(null); setJobId(''); selectedRun.current=''; location.hash='';
      }
      throw Error(
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail),
      );
    }
    return data;
  }
  async function guarded(fn: () => Promise<void>) {
    setWorking(true);
    setError("");
    setMessage("");
    try {
      await fn();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setWorking(false);
    }
  }
  async function refresh() {
    const [c, j, l, a, me] = await Promise.all([
      api("/cases"),
      api("/jobs"),
      api("/library"),
      api("/audit"), api("/me"),
    ]);
    setUser(me);
    setCases(c);
    setJobs(j);
    setLibrary(l);
    setAudit(a);
    setForm(
      (old) => old || c.find((x: RecordInput) => x.case_id === "QC002") || c[0],
    );
  }
  async function openJob(id: string) {
    const before = selectedRun.current;
    selectedRun.current = id;
    try {
      const d = await api("/jobs/" + id);
      const available = cases.length ? cases : await api("/cases");
      if (selectedRun.current !== id) return;
      setJobId(id);
      setDetail(d);
      setForm(
        available.find((c: RecordInput) => c.case_id === d.job.case_id) || null,
      );
      setQuote("");
      location.hash = "run=" + id;
    } catch (e) {
      if (selectedRun.current === id) selectedRun.current = before;
      throw e;
    }
  }
  useEffect(() => {
    api("/me")
      .then((u) => {
        setUser(u);
        setCsrf(u.csrf);
      })
      .catch(() => {})
      .finally(() => setAuthReady(true));
  }, []);
  useEffect(() => {
    if (user) {
      refresh().catch((e) => setError(e.message));
      const id = new URLSearchParams(location.hash.slice(1)).get("run");
      if (id) openJob(id).catch((e) => setError(e.message));
    }
  }, [user?.name]);
  useEffect(() => {
    if (!user || !busy) return;
    let active = true;
    const timer = setInterval(() => {
      Promise.all([
        api("/jobs"),
        jobId ? api("/jobs/" + jobId) : Promise.resolve(null),
      ])
        .then(([j, d]) => {
          if (!active) return;
          setJobs(j);
          if (d && selectedRun.current === jobId) setDetail(d);
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    }, 1500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [user?.name, busy, jobId, csrf]);
  useEffect(() => {
    if (!result) return;
    const prior = detail?.reviews
      .filter((r: Data) => (r.payload.issue_id || null) === currentIssueId)
      .at(-1)?.payload.final;
    const f = reviewTarget.findings?.[0];
    setHuman(
      prior?.origin === "human_edited_not_ai_prediction"
        ? prior
        : {
            status: resolved ? reviewTarget.status : "needs_information",
            l3_id: f?.l3_id || null,
            risk: f?.risk || (resolved ? "不适用" : "待定"),
            suggested_decision:
              f?.suggested_decision || (resolved ? "本项未命中" : "待补充"),
            explanation:
              reviewTarget.explanation_draft?.text || reviewTarget.reason || "",
            suggested_action:
              f?.suggested_action || "请核对原文与适用规则，仅限本项检查。",
          },
    );
    setAttested(false);
    setReason("");
    setReviewAction(
      result.schema_version === "m4-v1" && !result.issues?.length
        ? "return"
        : "edit",
    );
  }, [result?.run_id, detail?.reviews.length, currentIssueId]);
  // Tools only read/navigate; human attestation and publication are deliberately not agent tools.
  useEffect(() => {
    const context = (document as any).modelContext;
    if (!user || !context?.registerTool) return;
    const life = new AbortController();
    const register = (tool: any) => {
      try {
        Promise.resolve(
          context.registerTool(tool, { signal: life.signal }),
        ).catch(() => {});
      } catch {}
    };
    register({
      name: "list_saved_qc_runs",
      description:
        "Read saved synthetic QC run summaries; no model invocation or review.",
      inputSchema: {
        type: "object",
        properties: {},
        additionalProperties: false,
      },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: async (input: unknown) => {
        if (!input || typeof input !== "object" || Object.keys(input).length)
          throw Error("Expected empty object");
        return api("/jobs");
      },
    });
    register({
      name: "open_saved_qc_run",
      description:
        "Open a saved synthetic run in the visible workbench. Navigation only; does not approve it.",
      inputSchema: {
        type: "object",
        properties: { id: { type: "string" } },
        required: ["id"],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: true },
      execute: async (input: Data) => {
        if (
          !input ||
          typeof input.id !== "string" ||
          Object.keys(input).join() !== "id"
        )
          throw Error("Expected only id");
        await openJob(input.id);
        setPage("records");
        return { opened: input.id };
      },
    });
    return () => life.abort();
  }, [user?.name, csrf]);
  async function analyze() {
    if (!form) return;
    const saved = cases.find((c) => c.case_id === form.case_id);
    if (JSON.stringify(saved) !== JSON.stringify(form))
      throw Error("原文或上下文已改变，请先“另存为新记录”，再分析。");
    setMessage('云端正在拆分问题并生成解释，通常需要几秒到几十秒。请保持页面打开；不会自动重试。');
    const j = await api("/jobs", { case_id: form.case_id, request_key: uid() });
    setMessage('分析已保存。请核对证据和 AI 解释草稿。');
    await openJob(j.id);
    await refresh();
  }
  async function saveCopy() {
    if (!form) return;
    const id = "SYN-WEB-" + uid().slice(0, 8);
    await api("/cases", { ...form, case_id: id });
    setForm({ ...form, case_id: id });
    await refresh();
    selectedRun.current = "";
    setJobId("");
    setDetail(null);
    location.hash = "";
    setMessage("已另存新原文，旧记录未改变。现在可以分析。");
  }
  async function saveReview() {
    if (detail?.job.id !== jobId || selectedRun.current !== jobId)
      throw Error("运行选择已改变，请重新打开后复核。");
    const final =
      reviewAction === "edit"
        ? {
            status: human.status,
            l3_id: human.l3_id || null,
            risk: human.risk,
            suggested_decision: human.suggested_decision,
            explanation: human.explanation,
            suggested_action: human.suggested_action,
          }
        : null;
    const id = detail.job.id;
    const out = await api("/jobs/" + id + "/reviews", {
      action: reviewAction,
      reason,
      attested,
      expected_revision: detail?.reviews.length || 0,
      request_key: uid(),
      final,
      issue_id: currentIssueId,
    });
    await openJob(id);
    await refresh();
    setMessage("人工复核第 " + out.revision + " 版已保存。原初判保持不变。");
  }
  function highlight(text: string) {
    const at = quote
      ? quoteAt !== null &&
        text.slice(quoteAt, quoteAt + quote.length) === quote
        ? quoteAt
        : text.indexOf(quote)
      : -1;
    return at < 0 ? (
      text
    ) : (
      <>
        {text.slice(0, at)}
        <mark>{quote}</mark>
        {text.slice(at + quote.length)}
      </>
    );
  }
  const nav = (
    <nav>
      {[
        ["records", "质控记录"],
        ["reviews", "人工复核"],
        ["rules", "规则库"],
        ["audit", "我的复核历史"],
      ].map(([id, title]) => (
        <button
          key={id}
          onClick={() => setPage(id)}
          className={page === id ? "active" : ""}
        >
          {title}
        </button>
      ))}
    </nav>
  );
  const sidebar = (
    <aside>
      <div className="brand">
        QC<span>质控工作台</span>
      </div>
      <p>CLOUD / LIVE AI</p>
      {user && nav}
      <small>
        合成数据 · 非临床用途
        <br />
        不代表真实医院准确率
      </small>
    </aside>
  );
  if (!authReady)
    return (
      <div className="shell">
        {sidebar}
        <main>正在恢复访客会话…</main>
      </div>
    );
  if (!user) return (
    <div className="shell">{sidebar}<main>
      <header><div><p className="eyebrow">CLOUD / LIVE QWEN</p><h1>临床质控 · 在线体验</h1></div><span className="badge">免费演示</span></header>
      <div className="columns">
        <section className="panel"><h2>从一条记录开始</h2>
          <p>输入合成质控记录，让云端 Qwen 拆分问题；由程序核对规则、计算并给出三级分类。你可以查看证据、修改人工意见并保存。</p>
          <div className="notice">仅用于虚拟数据演示。不要输入姓名、身份证、联系方式或真实患者与试验资料。数据会发送至 Cloudflare 云端处理，不是本机分析。</div>
          <p>点击下方按钮，表示你同意仅使用合成数据。</p>
          <button className="primary" disabled={working} onClick={()=>guarded(async()=>{
            const u=await api('/session',{synthetic_only:true}); setUser(u); setCsrf(u.csrf);
          })}>{working?'正在建立访客空间…':'同意并开始体验'}</button>
          {error&&<p className="error" role="alert">{error}</p>}
        </section>
        <section className="panel"><h2>这个版本如何工作？</h2>
          <ol><li>选择 PK、访视、AE、库存、授权或 EDC 示例，也可编辑后另存。</li>
          <li>真正调用云端 Qwen，显示程序计算与原文、规则证据。</li>
          <li>逐项复核、保存修改，重新打开比较 AI 初判与人工版本。</li></ol>
          <p className="muted">每位访客每天最多 6 次，全站每天最多 40 次分析（UTC 日切）；云端免费额度也可能提前用尽，届时暂停，不转付费。单条最多 2400 字。</p>
          <p className="muted">访客空间不要求注册，仅限此浏览器访问；凭证有效期 7 天。清除浏览器 Cookie 或过期后无法恢复旧记录，请及时导出。复核按钮仅演示流程，不代表专业身份验证或医学签字。</p>
        </section>
      </div>
    </main></div>
  );
  const latest = detail?.reviews
    .filter((r: Data) => (r.payload.issue_id || null) === currentIssueId)
    .at(-1);
  return (
    <div className="shell">
      {sidebar}
      <main>
        <header>
          <div>
            <p className="eyebrow">合成研究 / 六类问题 · 逐项复核</p>
            <h1>
              {
                (
                  {
                    records: "质控记录",
                    evaluation: "合成评测",
                    reviews: "人工复核队列",
                    rules: "虚拟规则库",
                    audit: "操作历史",
                  } as Data
                )[page]
              }
            </h1>
          </div>
          <div className="actions">
            <span className="badge">
              {display(user.role)} · {user.name}
            </span>
            <span className="badge">今日个人剩余 {user.remaining ?? '—'} 次</span>
          </div>
        </header>
        <div className="notice">
          合成数据 / 非临床用途 · 云端 Qwen + Python 规则。禁止真实患者资料；每位访客的记录独立，规则库只读。
        </div>
        {error && (
          <div role="alert" className="error">
            {error}
          </div>
        )}
        {message && (
          <div role="status" className="success">
            {message}
          </div>
        )}
        {page === "records" && (
          <div className="columns">
            <section className="panel">
              <h2>01 · 输入原文</h2>
              <label>
                选择合成案例
                <select
                  value={form?.case_id || ""}
                  onChange={(e) => {
                    setForm(
                      cases.find((c) => c.case_id === e.target.value) || null,
                    );
                    selectedRun.current = "";
                    setJobId("");
                    setDetail(null);
                    location.hash = "";
                    setQuote("");
                    setMessage("");
                  }}
                >
                  {cases.map((c) => (
                    <option key={c.case_id}>{c.case_id}</option>
                  ))}
                </select>
              </label>
              {form && (
                <>
                  <div className="fieldgrid">
                    {[
                      ["study_id", "研究编号"],
                      ["site_id", "中心编号"],
                      ["protocol_id", "方案版本"],
                      ["event_at", "事件时点（含时区）"],
                    ].map(([key, title]) => (
                      <label key={key}>
                        {title}
                        <input
                          value={(form as any)[key]}
                          onChange={(e) =>
                            setForm({ ...form, [key]: e.target.value })
                          }
                          readOnly={!writable}
                        />
                      </label>
                    ))}
                  </div>
                  <label>
                    合成质控原文
                    <textarea
                      rows={7}
                      value={form.text}
                      onChange={(e) =>
                        setForm({ ...form, text: e.target.value })
                      }
                      readOnly={!writable}
                    />
                  </label>
                  <p className="muted">
                    改变原文后请另存；不会覆盖旧记录。保留“【合成虚拟记录】”及
                    SYN- 编号，不输入真实病历。
                  </p>
                  <div className="actions">
                    <button
                      className="primary"
                      disabled={!writable || working || busy}
                      onClick={() => guarded(analyze)}
                    >
                      {working ? "正在处理，请稍候…" : busy ? "云端分析进行中" : "运行云端 Qwen"}
                    </button>
                    <button
                      disabled={!writable || working}
                      onClick={() => guarded(saveCopy)}
                    >
                      另存为新记录
                    </button>
                  </div>
                </>
              )}
              <h3 className="sectionbreak">此记录的已保存运行</h3>
              {jobs.filter((j) => j.case_id === form?.case_id).length === 0 ? (
                <p className="muted">
                  尚无网页运行。点击分析将进行真实模型调用，不读取预期答案。
                </p>
              ) : (
                jobs
                  .filter((j) => j.case_id === form?.case_id)
                  .map((j) => (
                    <button
                      className={"runrow " + (j.id === jobId ? "chosen" : "")}
                      key={j.id}
                      onClick={() => guarded(() => openJob(j.id))}
                    >
                      <span>{time(j.created_at)}</span>
                      <span>
                        {display(j.state)} ·{" "}
                        {j.review_revision
                          ? "复核 v" + j.review_revision
                          : "未复核"}
                      </span>
                    </button>
                  ))
              )}
            </section>
            <section className="panel">
              <h2>02 · 初判、证据与人工意见</h2>
              {!detail ? (
                <div className="empty">
                  先选一条记录运行分析
                  <br />
                  <small>或打开一条已保存运行。</small>
                </div>
              ) : (
                <>
                  <div className="resulthead">
                    <strong>
                      {result
                        ? display(result.status)
                        : display(detail.job.state)}
                    </strong>
                    <span className="badge">
                      {result
                        ? result.mode === "live_cloud"
                          ? "已保存 · 真实云端运行"
                          : "测试替身 · 非真实推理"
                        : "实时运行"}
                    </span>
                  </div>
                  {["running", "queued"].includes(detail.job.state) && (
                    <div className="progress" role="status">
                      <span className="spinner" />
                      {stages[detail.job.stage] || detail.job.stage}
                      <p className="muted">
                        抽取事实和组织解释分别调用模型，请稍候。不会自动换成预设答案。
                      </p>
                    </div>
                  )}
                  {detail.job.error && (
                    <p className="error">{detail.job.error}</p>
                  )}
                  {result && (
                    <>
                      <p className="muted">
                        {time(result.started_at)} ·{" "}
                        {(result.elapsed_ms / 1000).toFixed(1)} 秒 · 规则快照{" "}
                        {detail.job.bundle_id}
                      </p>
                      <div ref={evidenceRef} className="source">
                        <strong>本次分析实际使用的原文</strong>
                        <p>{highlight(result.input.text)}</p>
                      </div>
                      {result.issues?.length > 0 && (
                        <div className="sectionbreak">
                          <h3>逐项检查（{result.issues.length} 项）</h3>
                          {result.issues.map((item: Data) => (
                            <div className="resultbox" key={item.issue_id}>
                              <strong>
                                {item.issue_id} · {item.family_name} ·{" "}
                                {display(item.status)}
                              </strong>
                              <p>{item.reason}</p>
                              <p>
                                建议风险：{item.risk} ·{" "}
                                {item.triage_priority === "priority"
                                  ? "优先处理"
                                  : "常规复核"}
                              </p>
                              {item.explanation_draft && (
                                <div className="draft">
                                  <strong>本项 Qwen 草稿 · 语义待核实</strong>
                                  <p>{item.explanation_draft.text}</p>
                                </div>
                              )}
                              <details>
                                <summary>本项事实与规则检索</summary>
                                <Json
                                  data={{
                                    facts: item.facts,
                                    rule_keys: item.rule_keys,
                                    retrieval: item.retrieval,
                                  }}
                                />
                              </details>
                              <button
                                onClick={() => {
                                  setIssueId(item.issue_id);
                                  setReason("");
                                  setAttested(false);
                                }}
                              >
                                {currentIssueId === item.issue_id
                                  ? "正在复核此项"
                                  : "选择复核此项"}
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                      {result.explanation_error && (
                        <p className="error">
                          解释生成/校验未完成：{result.explanation_error}
                          。已完成的程序检查保留，不能一键确认。
                        </p>
                      )}
                      <div className="resultbox">
                        <h3>程序确定结果</h3>
                        <p>{result.reason}</p>
                        <p>
                          建议风险：<strong>{result.risk}</strong>　处理优先级：
                          {result.triage_priority === "priority"
                            ? "优先人工处理"
                            : "常规复核"}
                        </p>
                        {result.calculation && (
                          <div className="calculation">
                            {result.calculation.actual_minutes}{" "}
                            <span>
                              分钟 / 最低要求{" "}
                              {result.calculation.minimum_minutes} 分钟
                            </span>
                          </div>
                        )}
                        {result.findings?.map((f: Data, i: number) => (
                          <div key={i}>
                            <p>
                              {[f.l1, f.l2, f.l3].filter(Boolean).join(" / ") ||
                                f.l3_id}
                            </p>
                            <p>建议判定：{f.suggested_decision}</p>
                            <p>建议措施：{f.suggested_action}</p>
                            <details>
                              <summary>L4 仅参考候选</summary>
                              <Json data={f.l4_candidates} />
                            </details>
                          </div>
                        ))}
                      </div>
                      {result.status === "analysis_failed" &&
                        result.deterministic_decision && (
                          <details>
                            <summary>
                              失败前已完成的计算（不代表完整分析成功）
                            </summary>
                            <Json data={result.deterministic_decision} />
                          </details>
                        )}
                      <h3>证据：点击原文引文定位</h3>
                      {(
                        result.evidence ||
                        result.validated_extraction?.evidence ||
                        []
                      ).map((e: Data) => (
                        <div className="evidence" key={e.evidence_id}>
                          <button
                            disabled={e.origin === "rule"}
                            onClick={() => {
                              setQuote(e.quote);
                              setQuoteAt(e.start_char ?? null);
                              evidenceRef.current?.scrollIntoView({
                                behavior: "smooth",
                                block: "center",
                              });
                            }}
                          >
                            [{e.evidence_id}]
                          </button>
                          <div>
                            <p>{e.quote}</p>
                            <small>
                              {e.document_id
                                ? e.document_id + " / " + e.section_id
                                : "原记录逐字引文"}{" "}
                              {e.start_char !== undefined
                                ? `字符位置 ${e.start_char}–${e.end_char}`
                                : ""}
                            </small>
                          </div>
                        </div>
                      ))}
                      {result.explanation_draft && (
                        <div className="draft">
                          <h3>Qwen 解释草稿 · 语义未核实</h3>
                          <p>{result.explanation_draft.text}</p>
                          <small>
                            引用存在不代表推理正确。请特别核对“符合/不符合”与计算是否一致。
                          </small>
                        </div>
                      )}
                      <details>
                        <summary>查看步骤耗时与运行标识</summary>
                        {result.steps.map((s: Data, i: number) => (
                          <p key={i}>
                            {stages[s.name] || s.name}：{s.status} / {s.wall_ms}{" "}
                            ms
                          </p>
                        ))}
                        <p>初判摘要：{detail.result_sha256}</p>
                        <p>模型：{result.model_identity?.model}</p>
                        <p>运行ID：{result.run_id}</p>
                        <p>提示词：{result.prompt_version}</p>
                        <a href={"/api/jobs/" + jobId + "/export"}>
                          导出合成运行及复核历史 JSON
                        </a>
                      </details>
                      <div className="sectionbreak">
                        <h3>
                          人工复核{" "}
                          {currentIssueId ? `· 当前 ${currentIssueId} ` : ""}
                          {latest ? "· 当前 v" + latest.revision : "· 尚未保存"}
                        </h3>
                        {latest && (
                          <div className="success">
                            {display(latest.payload.action)} · {latest.actor} ·{" "}
                            {time(latest.created_at)}
                            <p>{latest.payload.reason}</p>
                            <details>
                              <summary>
                                当前人工结果（原初判在上方保留）
                              </summary>
                              <Json data={latest.payload.final} />
                            </details>
                          </div>
                        )}
                        {writable ? (
                          <>
                            {result.issues?.length > 0 && (
                              <label>
                                选择需要复核的问题项
                                <select
                                  value={currentIssueId || ""}
                                  onChange={(e) => setIssueId(e.target.value)}
                                >
                                  {result.issues.map((i: Data) => (
                                    <option key={i.issue_id} value={i.issue_id}>
                                      {i.issue_id} · {i.family_name} ·{" "}
                                      {display(i.status)}
                                    </option>
                                  ))}
                                </select>
                                <p className="muted">
                                  每项单独保存。只有所有问题项都处理完毕，整条提醒才会关闭。
                                </p>
                              </label>
                            )}
                            <label>
                              本次操作
                              <select
                                value={reviewAction}
                                onChange={(e) =>
                                  setReviewAction(e.target.value)
                                }
                              >
                                <option
                                  disabled={
                                    result.schema_version === "m4-v1" &&
                                    !result.issues?.length
                                  }
                                  value="edit"
                                >
                                  编辑人工意见后保存
                                </option>
                                <option disabled={!resolved} value="confirm">
                                  已核验{currentIssueId ? "本项" : "全部"}
                                  初判和草稿，确认原初判
                                </option>
                                <option value="return">
                                  退回补充信息（不确认结论）
                                </option>
                              </select>
                            </label>
                            {reviewAction === "edit" && (
                              <>
                                <div className="fieldgrid">
                                  <label>
                                    人工处理状态
                                    <select
                                      value={human.status || ""}
                                      onChange={(e) => {
                                        const s = e.target.value;
                                        setHuman({
                                          ...human,
                                          status: s,
                                          l3_id:
                                            s === "proposed_findings"
                                              ? human.l3_id
                                              : null,
                                          risk:
                                            s === "needs_information"
                                              ? "待定"
                                              : s ===
                                                  "no_finding_for_checked_rule"
                                                ? "不适用"
                                                : "中",
                                          suggested_decision:
                                            s === "needs_information"
                                              ? "待补充"
                                              : s ===
                                                  "no_finding_for_checked_rule"
                                                ? "本项未命中"
                                                : "需人工判定",
                                        });
                                      }}
                                    >
                                      <option value="proposed_findings">
                                        发现问题
                                      </option>
                                      <option value="no_finding_for_checked_rule">
                                        本项未命中
                                      </option>
                                      <option value="needs_information">
                                        待补充/待定
                                      </option>
                                    </select>
                                  </label>
                                  <label>
                                    人工风险
                                    <select
                                      value={human.risk || "待定"}
                                      onChange={(e) =>
                                        setHuman({
                                          ...human,
                                          risk: e.target.value,
                                        })
                                      }
                                    >
                                      {["低", "中", "高", "待定", "不适用"].map(
                                        (x) => (
                                          <option key={x}>{x}</option>
                                        ),
                                      )}
                                    </select>
                                  </label>
                                </div>
                                <label>
                                  人工 L3（上级路径由后台回填）
                                  <select
                                    disabled={
                                      human.status !== "proposed_findings"
                                    }
                                    value={human.l3_id || ""}
                                    onChange={(e) =>
                                      setHuman({
                                        ...human,
                                        l3_id: e.target.value || null,
                                      })
                                    }
                                  >
                                    <option value="">不分类 / 未确定</option>
                                    {result.knowledge_snapshot?.taxonomy.paths.map(
                                      (p: Data) => (
                                        <option key={p.l3_id} value={p.l3_id}>
                                          {p.l1} / {p.l2} / {p.l3}
                                        </option>
                                      ),
                                    )}
                                  </select>
                                </label>
                                <label>
                                  人工建议判定
                                  <select
                                    value={
                                      human.suggested_decision || "需人工判定"
                                    }
                                    onChange={(e) =>
                                      setHuman({
                                        ...human,
                                        suggested_decision: e.target.value,
                                      })
                                    }
                                  >
                                    {(human.status === "proposed_findings"
                                      ? ["方案偏离", "需人工判定"]
                                      : human.status === "needs_information"
                                        ? ["待补充"]
                                        : ["本项未命中"]
                                    ).map((x) => (
                                      <option key={x}>{x}</option>
                                    ))}
                                  </select>
                                </label>
                                <label>
                                  人工解释
                                  <textarea
                                    rows={4}
                                    value={human.explanation || ""}
                                    onChange={(e) =>
                                      setHuman({
                                        ...human,
                                        explanation: e.target.value,
                                      })
                                    }
                                  />
                                </label>
                                <label>
                                  人工建议措施
                                  <textarea
                                    rows={2}
                                    value={human.suggested_action || ""}
                                    onChange={(e) =>
                                      setHuman({
                                        ...human,
                                        suggested_action: e.target.value,
                                      })
                                    }
                                  />
                                </label>
                              </>
                            )}
                            <label>
                              操作理由（必填，保留到历史中）
                              <textarea
                                rows={2}
                                value={reason}
                                onChange={(e) => setReason(e.target.value)}
                                placeholder="例如：核对27<30后，纠正模型草稿中‘符合规定’的措辞。"
                              />
                            </label>
                            <label className="check">
                              <input
                                type="checkbox"
                                checked={attested}
                                onChange={(e) => setAttested(e.target.checked)}
                              />
                              我已核对本次原文、适用规则与意见；此操作仅用于合成演示。
                            </label>
                            <button
                              disabled={
                                working || !attested || reason.trim().length < 3
                              }
                              className="primary"
                              onClick={() => guarded(saveReview)}
                            >
                              保存新的复核版本
                            </button>
                          </>
                        ) : (
                          <p className="muted">
                            此账户只能查看人工结果。复核请使用 reviewer 账户。
                          </p>
                        )}
                        {detail.reviews.length > 0 && (
                          <details>
                            <summary>
                              全部复核历史（{detail.reviews.length} 版）
                            </summary>
                            {detail.reviews.map((r: Data) => (
                              <div className="history" key={r.revision}>
                                <strong>
                                  v{r.revision} ·{" "}
                                  {r.payload.issue_id || "整条记录"} ·{" "}
                                  {display(r.payload.action)}
                                </strong>
                                <p>
                                  {r.actor} / {time(r.created_at)}
                                </p>
                                <p>{r.payload.reason}</p>
                                <Json data={r.payload.final} />
                              </div>
                            ))}
                          </details>
                        )}
                      </div>
                    </>
                  )}
                </>
              )}
            </section>
          </div>
        )}
        {page === "reviews" && (
          <section className="panel">
            <div className="actions spread">
              <h2>
                {
                  jobs.filter(
                    (j) => j.alert_state && j.alert_state !== "closed",
                  ).length
                }{" "}
                条待处理 / 已转交
              </h2>
              <button onClick={() => guarded(refresh)}>刷新</button>
            </div>
            <p className="muted">
              本地提醒不发送临床通知。关闭表示本次人工处理结束，不等于医学风险已排除。
            </p>
            {jobs.length === 0 ? (
              <div className="empty">
                尚无网页分析；先到质控记录页运行 QC002。
              </div>
            ) : (
              <div className="tablewrap">
                <table>
                  <thead>
                    <tr>
                      <th>记录 / 时间</th>
                      <th>初判风险</th>
                      <th>优先级</th>
                      <th>提醒状态</th>
                      <th>人工版本</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...jobs]
                      .sort(
                        (a, b) =>
                          (a.alert_state === "closed" ? 1 : 0) -
                            (b.alert_state === "closed" ? 1 : 0) ||
                          (a.priority === "priority" ? -1 : 0) -
                            (b.priority === "priority" ? -1 : 0),
                      )
                      .map((j) => (
                        <tr key={j.id}>
                          <td>
                            <strong>{j.case_id}</strong>
                            <small>{time(j.created_at)}</small>
                          </td>
                          <td>{j.risk}</td>
                          <td>{j.priority === "priority" ? "优先" : "常规"}</td>
                          <td>{display(j.alert_state || j.state)}</td>
                          <td>
                            {j.review_revision
                              ? "v" +
                                j.review_revision +
                                " / " +
                                display(j.review_action)
                              : "未复核"}
                          </td>
                          <td>
                            <button
                              onClick={() =>
                                guarded(async () => {
                                  await openJob(j.id);
                                  setForm(
                                    cases.find(
                                      (c) => c.case_id === j.case_id,
                                    ) || null,
                                  );
                                  setPage("records");
                                })
                              }
                            >
                              打开
                            </button>
                            {writable &&
                              j.alert_state &&
                              j.alert_state !== "closed" && (
                                <button
                                  onClick={() =>
                                    guarded(async () => {
                                      await api("/jobs/" + j.id + "/alerts", {
                                        state: "seen",
                                        reason: "复核者已查看工作台提醒",
                                      });
                                      await refresh();
                                    })
                                  }
                                >
                                  标记已查看
                                </button>
                              )}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}
        {page === "rules" && library && (
          <section className="panel">
            <h2>已发布：{library.knowledge.rules.rules.length} 条规则版本</h2>
            <p className="muted">
              六类有限中文事实语法可分析。规则和来源均为虚拟研究资料；冲突研究仅用于边界演示。
            </p>
            <details>
              <summary>Demo 分类树 · L4 只是候选</summary>
              {library.knowledge.taxonomy.paths.map((p: Data) => (
                <p key={p.l3_id}>
                  {p.l1} / {p.l2} / {p.l3}
                  <br />
                  <small>
                    {p.l4_candidates.map((x: Data) => x.label).join("；")}
                  </small>
                </p>
              ))}
            </details>
            {library.knowledge.rules.rules.map((r: Data) => (
              <details key={r.rule_id + "@" + r.version}>
                <summary>
                  {r.rule_id} @{r.version}　{r.scope.study_id}
                </summary>
                <p>{r.source.quote}</p>
                <div className="fieldgrid">
                  <div>
                    <strong>适用范围</strong>
                    <Json data={r.scope} />
                  </div>
                  <div>
                    <strong>机器条件 / 替代关系</strong>
                    <Json
                      data={{
                        parameters: r.parameters,
                        supersedes: r.supersedes,
                        source:
                          r.source.document_id + " / " + r.source.section_id,
                      }}
                    />
                  </div>
                </div>
              </details>
            ))}
            <p className="muted">公开版规则只读。每次分析绑定当时的固定规则快照；访客不能修改全站规则。</p>
          </section>
        )}
        {page === "audit" && (
          <section className="panel">
            <h2>我的复核历史</h2>
            <p className="muted">
              仅展示当前访客的复核历史。AI 初判不被人工修改覆盖；这是匿名演示记录，不是监管级审计或电子签名。
            </p>
            <button onClick={() => guarded(refresh)}>刷新历史</button>
            <div className="tablewrap">
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>操作者</th>
                    <th>操作</th>
                    <th>目标 / 细节</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.map((a) => (
                    <tr key={a.id}>
                      <td>{time(a.created_at)}</td>
                      <td>{a.actor}</td>
                      <td>{a.action}</td>
                      <td>
                        <code>{a.target}</code>
                        <details>
                          <summary>查看</summary>
                          <Json data={a.detail} />
                        </details>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
