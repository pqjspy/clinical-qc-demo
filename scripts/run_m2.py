"""Run a real local Qwen workflow; no answer lookup or fallback preview."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinical_qc_demo.contracts import CaseRecord
from clinical_qc_demo.data import load_records
from clinical_qc_demo.local_model import OllamaClient, decode_json
from clinical_qc_demo.workflow import analyze_record


def show(result):
    print(f"\n模式：{result['mode']}（真实本机请求；不是M1参考答案预览）")
    print(f"运行ID：{result['run_id']}")
    print("\n1. 用户原文\n" + result["input"]["text"])
    print("\n2. Qwen实际抽取（原文片段）")
    print(json.dumps(result.get("extraction"), ensure_ascii=False, indent=2))
    print("\n3. 规则检索")
    retrieval = result.get("retrieval", {})
    for item in retrieval.get("ranking", []):
        print(f"{item['rule_key']}  BM25={item['score']:.4f}")
    if not retrieval:
        print("未进入检索；先处理范围、缺信息或模型错误。")
    print("\n4. Python计算与规则建议")
    decision = result.get("deterministic_decision", {})
    print(json.dumps(decision.get("calculation"), ensure_ascii=False, indent=2))
    print("运行状态：" + result["status"])
    print(result.get("reason", ""))
    for finding in result["findings"]:
        print(" / ".join(finding[k] for k in ("l1", "l2", "l3")))
        print(f"建议风险：{finding['risk']}；建议判定：{finding['suggested_decision']}；需人工复核")
        print("建议措施：" + finding["suggested_action"])
    for evidence in result.get("evidence", []):
        if evidence["origin"] == "rule":
            print(f"规则原文 [{evidence['evidence_id']}]：{evidence['quote']}")
    print("\n5. Qwen解释草稿（语义尚需人工复核，不改变规则结果）")
    print(result.get("explanation_draft", {}).get("text", "未生成/未通过校验。"))
    if result.get("error"):
        print("错误：" + json.dumps(result["error"], ensure_ascii=False))
        if decision.get("findings"):
            print("确定计算已产生待复核问题项，但后续步骤失败；完整AI结果未发布。该计算和问题项保留在deterministic_decision。")
    print(f"\n实际模型请求次数：{result['model_call_count']}；总耗时：{result['elapsed_ms']/1000:.2f}秒")
    print("完整本地记录：" + result["run_directory"] + "/result.json")
    print("仅合成演示；尚未临床验证；非PK/多问题未实现；没有读取参考答案。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--case", help="已有虚拟案例ID，只用于读取输入，不送入模型")
    group.add_argument("--input-json", type=Path, help="一条CaseRecord JSON，不含参考答案")
    parser.add_argument("--readable", action="store_true")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.input_json:
        record = CaseRecord.model_validate(decode_json(args.input_json.read_text(encoding="utf-8")))
    else:
        case_id = args.case or "QC002"
        record = next((r for r in load_records(ROOT) if r.case_id == case_id), None)
        if record is None:
            parser.error(f"Unknown input case: {case_id}")
    result = analyze_record(ROOT, record, client=OllamaClient(timeout=args.timeout),
                            progress=lambda name: print(f"正在执行：{name}", file=sys.stderr, flush=True))
    if args.readable:
        show(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "analysis_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
