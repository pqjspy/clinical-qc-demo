import "./retrieval.css";

type Ranking = { document_id: string; rank?: number; score?: number };
type Document = { id: string; text: string };
type ModelCall = {
  stage?: string;
  model?: string;
  status?: string;
  wall_ms?: number;
};
type Method = "bm25" | "dense" | "hybrid" | "hybrid_reranked";

const methods: { key: Method; label: string; score: string }[] = [
  { key: "bm25", label: "BM25", score: "词项分数" },
  { key: "dense", label: "Dense", score: "向量相似度" },
  { key: "hybrid", label: "Hybrid", score: "RRF 融合分数" },
  { key: "hybrid_reranked", label: "Reranker", score: "重排分数" },
];

const statuses: Record<string, string> = {
  started: "已开始 · 结果未完成",
  completed: "已完成",
  success: "成功",
  succeeded: "成功",
  ok: "成功",
  failed: "失败",
  error: "失败",
  unavailable: "不可用",
  not_configured: "未配置",
  disabled: "未启用",
  skipped: "未执行",
  not_applicable: "不适用",
  not_requested: "未请求",
};

function statusText(value: unknown): string {
  return typeof value === "string"
    ? statuses[value] || value
    : "未记录状态";
}

function numberText(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? String(value)
    : "未记录";
}

function scoreText(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未返回分数";
  return value !== 0 && Math.abs(value) < 0.0001
    ? value.toExponential(3)
    : value.toFixed(4);
}

export default function RetrievalComparison({ trace }: { trace: any }) {
  if (!trace || typeof trace !== "object") return null;

  const comparison = trace.comparison;
  const usedAsContext = trace.used_in === "decomposition_context";
  const retrievalMode = comparison?.mode || trace.mode;
  const retrievalConfig = comparison?.config || trace.configuration;
  const documents: Document[] = Array.isArray(trace.documents)
    ? trace.documents.filter(
        (document: any) => document && typeof document.id === "string",
      )
    : [];
  const calls: ModelCall[] = Array.isArray(trace.calls)
    ? trace.calls.filter((call: any) => call && typeof call === "object")
    : [];
  const enhancedModelsNotRun = trace.status === "not_applicable" && calls.length === 0;
  const eligibleIds: string[] | null = Array.isArray(trace.scope?.eligible_rule_ids)
    ? trace.scope.eligible_rule_ids.filter((id: unknown) => typeof id === "string")
    : null;
  const selectedIds: string[] = Array.isArray(comparison?.selected_ids)
    ? [...new Set<string>(comparison.selected_ids.filter((id: unknown) => typeof id === "string"))]
    : [];
  const candidateIds: string[] | null = Array.isArray(comparison?.candidate_ids)
    ? [...new Set<string>(comparison.candidate_ids.filter((id: unknown) => typeof id === "string"))]
    : null;
  const rankings = Object.fromEntries(
    methods.map(({ key }) => [
      key,
      Array.isArray(comparison?.rankings?.[key])
        ? comparison.rankings[key].filter(
            (entry: any) => entry && typeof entry.document_id === "string",
          )
        : null,
    ]),
  ) as Record<Method, Ranking[] | null>;
  const documentById = new Map(documents.map((document) => [document.id, document]));
  const rowIds = [...new Set([
    ...documents.map((document) => document.id),
    ...methods.flatMap(({ key }) => (rankings[key] || []).map((entry) => entry.document_id)),
    ...selectedIds,
  ])];

  function methodStatus(key: Method) {
    if (enhancedModelsNotRun) return "增强比较未执行";
    const count = rankings[key]?.length;
    const countLabel = count === undefined ? "未返回排名" : `${count} 条排名`;
    if (key === "hybrid_reranked" && comparison?.reranker_status) {
      return `${statusText(comparison.reranker_status)} · ${countLabel}`;
    }
    if (key === "dense") {
      const callStates = [...new Set(calls
        .filter((call) => /embed|dense/i.test(call.stage || ""))
        .map((call) => statusText(call.status)))];
      if (callStates.length) return `${callStates.join(" / ")} · ${countLabel}`;
    }
    return countLabel;
  }

  return (
    <details className="retrieval-comparison">
      <summary>
        检索比较 · BM25 / Dense / Hybrid / Reranker
        <span className={`retrieval-status${trace.status === "failed" ? " retrieval-status-failed" : ""}`}>
          {statusText(trace.status)}
        </span>
      </summary>

      <p className="retrieval-note">
        本面板展示整次运行的增强检索比较；逐项 BM25 检索与确定性规则核验见各问题结果。
        增强排名的用途是为 Qwen 问题拆分提供上下文，不用于选择冲突规则或决定最终分类。
        当前语料是经过适用范围过滤的小型合成规则库，此比较不能代表大规模检索质量。
      </p>
      <p className={`retrieval-usage${usedAsContext ? " retrieval-usage-applied" : ""}`}>
        {usedAsContext
          ? "本次已将排名选中的规则用于 Qwen 问题拆分上下文。"
          : "尚未用于 Qwen 上下文。下方排名与选中的候选仅作检索过程展示。"}
      </p>
      {trace.status === "failed" && (
        <p className="retrieval-alert" role="status">
          检索比较失败。下方仅展示本次实际返回的数据；缺失的模型结果不计作零分。
          {typeof trace.error === "string" && <span>{trace.error}</span>}
        </p>
      )}
      {trace.status === "not_applicable" && (
        <p className="retrieval-note">
          本次检索比较不适用。
          {typeof trace.error === "string" && ` ${trace.error}`}
          {typeof trace.reason === "string" && ` ${trace.reason}`}
          {enhancedModelsNotRun && " 本次 Dense / Reranker 均未执行。"}
          {retrievalMode === "legacy_bm25_only" && " 本次使用原有逐项 BM25 检查流程。"}
        </p>
      )}

      {typeof trace.query === "string" && trace.query && (
        <div className="retrieval-query">
          <strong>本次检索文本</strong>
          <p>{trace.query}</p>
        </div>
      )}

      <dl className="retrieval-metadata">
        <div><dt>实际适用规则</dt><dd>{eligibleIds === null ? "未记录" : `${eligibleIds.length} 条`}</dd></div>
        <div><dt>范围外排除</dt><dd>{numberText(trace.scope?.excluded_count)}</dd></div>
        <div><dt>比较语料数</dt><dd>{numberText(comparison?.document_count)}</dd></div>
        <div><dt>候选 Top-K 上限</dt><dd>{numberText(retrievalConfig?.candidate_k)}</dd></div>
        <div><dt>实际候选数</dt><dd>{candidateIds === null ? "未记录" : candidateIds.length}</dd></div>
        <div><dt>上下文 Top-K 上限</dt><dd>{numberText(retrievalConfig?.final_k)}</dd></div>
        <div><dt>排名选中数</dt><dd>{Array.isArray(comparison?.selected_ids) ? selectedIds.length : "未记录"}</dd></div>
        <div><dt>用于 Qwen 上下文</dt><dd>{usedAsContext ? `${selectedIds.length} 条规则` : "尚未使用"}</dd></div>
        <div><dt>RRF 常数 K</dt><dd>{numberText(retrievalConfig?.rrf_k)}</dd></div>
      </dl>
      <p className="retrieval-note">
        Top-K 是数量上限，实际数量受适用规则与候选结果限制。
        分数不是概率，各方法的分数量纲不同，不可直接横向比较。
      </p>

      {rowIds.length > 0 ? (
        <div className="retrieval-table-scroll" role="region" aria-label="检索方法排名比较，可横向滚动" tabIndex={0}>
          <table className="retrieval-table">
            <caption>
              同一查询的规则排名与原始分数；
              {usedAsContext ? "“已用于上下文”表示本次已送入 Qwen。" : "“候选选中”仅表示排名选中，尚未用于 Qwen 上下文。"}
            </caption>
            <thead>
              <tr>
                <th scope="col">规则 / 原文摘要</th>
                {methods.map(({ key, label, score }) => (
                  <th scope="col" key={key}>
                    {label}
                    <span>{score}</span>
                    <span className="retrieval-method-status">{methodStatus(key)}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rowIds.map((id) => {
                const document = documentById.get(id);
                const quote = typeof document?.text === "string" ? document.text : "";
                return (
                  <tr key={id} className={selectedIds.includes(id) ? "retrieval-selected-row" : undefined}>
                    <th scope="row">
                      <code>{id}</code>
                      {selectedIds.includes(id) && <span className="retrieval-selected-label">{usedAsContext ? "已用于上下文" : "候选选中"}</span>}
                      <p>{quote ? `${quote.slice(0, 110)}${quote.length > 110 ? "…" : ""}` : "未返回规则原文"}</p>
                    </th>
                    {methods.map(({ key }) => {
                      const entry = rankings[key]?.find((item) => item.document_id === id);
                      return (
                        <td key={key}>
                          {entry ? (
                            <>
                              <strong>{typeof entry.rank === "number" && Number.isInteger(entry.rank) && entry.rank > 0 ? `#${entry.rank}` : "未返回名次"}</strong>
                              <span className="retrieval-score">{scoreText(entry.score)}</span>
                            </>
                          ) : (
                            <span className="retrieval-missing">{rankings[key]?.length ? "未列入排名" : "无可用排名"}</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <p className="retrieval-note">本次未返回可比较的规则排名。</p>}

      <section className="retrieval-sources" aria-label={usedAsContext ? "已用于 Qwen 上下文的规则原文" : "排名选中的候选规则原文"}>
        <h3>{usedAsContext ? "规则原文 · 已用于 Qwen 上下文" : "候选规则原文 · 尚未用于 Qwen 上下文"}</h3>
        {selectedIds.length ? selectedIds.map((id) => (
          <blockquote key={id}>
            <strong>{id}</strong>
            <p>{documentById.get(id)?.text || "本次记录未保存此规则的原文。"}</p>
          </blockquote>
        )) : <p className="retrieval-note">未记录排名选中的规则。</p>}
      </section>

      <section className="retrieval-models" aria-label="检索模型与调用耗时">
        <h3>模型与调用</h3>
        <dl className="retrieval-model-list">
          <div><dt>Embedding</dt><dd>{trace.configuration?.embedding_model || "未记录模型"}</dd></div>
          <div><dt>Reranker</dt><dd>{trace.configuration?.reranker_model || "未记录模型"}</dd></div>
          <div><dt>检索模式</dt><dd>{retrievalMode === "legacy_bm25_only" ? "原有逐项 BM25（增强检索未执行）" : retrievalMode || "未记录"}</dd></div>
          {comparison?.version && <div><dt>比较版本</dt><dd>{comparison.version}</dd></div>}
        </dl>
        {calls.length ? (
          <ul className="retrieval-calls">
            {calls.map((call, index) => (
              <li key={`${call.stage}-${index}`}>
                <span><strong>{call.stage || "未记录阶段"}</strong> · {call.model || "未记录模型"}</span>
                <span>{statusText(call.status)} · {typeof call.wall_ms === "number" && Number.isFinite(call.wall_ms) && call.wall_ms >= 0 ? `${call.wall_ms.toFixed(0)} ms` : "耗时未记录"}</span>
              </li>
            ))}
          </ul>
        ) : <p className="retrieval-note">未记录模型调用；不推断模型已运行或耗时为零。</p>}
      </section>
    </details>
  );
}
