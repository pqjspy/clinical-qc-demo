"""Read-only M1 fixture check, or explicitly labelled expected-answer preview."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from clinical_qc_demo.data import preview_case, validate_bundle


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', help='Show authored expected answers, NOT an AI prediction.')
    parser.add_argument('--readable', action='store_true', help='Use a short Chinese case walkthrough instead of JSON.')
    args = parser.parse_args()
    if args.readable and not args.case:
        parser.error('--readable requires --case')
    result = preview_case(ROOT, args.case) if args.case else validate_bundle(ROOT)
    if args.readable:
        print(result['notice'])
        print('\n1. 用户输入\n' + result['input']['text'])
        print('\n2. 预期处理状态\n' + result['expected_status'])
        print('\n3. 应提出的问题项')
        for row in result['expected_findings']:
            print(' / '.join(row[key] for key in ('一级分类', '二级分类', '三级分类')))
            print('建议风险：' + row['风险等级'] + '；建议判定：' + row['建议判定'] + '；需人工复核')
            for source in row['规则证据']:
                print('规则原文 [' + source['document_id'] + '/' + source['section_id'] + ']：' + source['quote'])
        if not result['expected_findings']:
            print('不输出违规分类；原因见下方。')
        print('\n4. 为什么\n' + result['explanation'])
        print('\n5. 作者给定数据的算术核对（不是模型抽取）')
        for item in result['arithmetic']:
            print(item['operation'] + ' = ' + item['value'])
        if not result['arithmetic']:
            print('本例无可复算的算术项。')
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
