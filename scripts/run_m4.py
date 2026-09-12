#!/usr/bin/env python3
"""Run the real six-family workflow, one demo input at a time."""
import argparse
import json
from pathlib import Path
from clinical_qc_demo.data import load_records
from clinical_qc_demo.contracts import CaseRecord
from clinical_qc_demo.m4_workflow import analyze_record

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', default='DEMO-VISIT-01')
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    record = CaseRecord.model_validate(json.loads(args.input.read_text())) if args.input else next(r for r in load_records(root) if r.case_id == args.case)
    result = analyze_record(root, record, progress=lambda s: print(s, flush=True))
    print(json.dumps({k: result.get(k) for k in ('run_id','status','risk','reason','elapsed_ms','run_directory')}, ensure_ascii=False, indent=2))
    for item in result['issues']:
        print(item['issue_id'], item['family_name'], item['status'], item['reason'])
    if result.get('error'):
        print(result['error'])
    return 1 if result['status'] == 'analysis_failed' else 0

if __name__ == '__main__':
    raise SystemExit(main())
