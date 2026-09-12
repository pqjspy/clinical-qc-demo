"""Build an allow-listed public bundle; never copy runtime or reference answers."""
import ast
import hashlib
import json
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEST = HERE / 'src' / 'core'
DEST.mkdir(parents=True, exist_ok=True)
(DEST / '__init__.py').write_text('')
hashes = {}
for name in ('contracts.py', 'data.py', 'retrieval.py', 'm4_engine.py'):
    path = ROOT / 'src' / 'clinical_qc_demo' / name
    hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    shutil.copyfile(path, DEST / name)

source = (ROOT / 'src' / 'clinical_qc_demo' / 'm4_workflow.py').read_text()
tree = ast.parse(source)
names = {'DraftItem', 'Drafts', 'decode_routing', 'validate_drafts'}
selected = [ast.get_source_segment(source, n) for n in tree.body if getattr(n, 'name', None) in names]
(DEST / 'transport.py').write_text('from pydantic import Field\nfrom .contracts import StrictModel\nfrom .m4_engine import Decomposition\n\n' + '\n\n'.join(selected) + '\n')
prompt = next(n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == 'prompt' for t in n.targets)
              and isinstance(n.value, ast.Constant))
knowledge = {k: json.loads((ROOT / 'data' / 'knowledge' / (k + '.json')).read_text())
             for k in ('taxonomy', 'protocols', 'rules')}
cases = json.loads((ROOT / 'data' / 'inputs' / 'cases.json').read_text())['records']
assert len(cases) == 12 and all(c['text'].startswith('【合成虚拟记录】') for c in cases)
payload = dict(knowledge=knowledge, cases=cases, implementation_sha256=hashes, routing_prompt=prompt)
(HERE / 'src' / 'bundled.py').write_text('import json\nBUNDLE = json.loads(' + repr(json.dumps(payload, ensure_ascii=False)) + ')\n')
print(f'Bundled {len(cases)} synthetic examples, {len(knowledge["rules"]["rules"])} rule versions; no accounts/history/answers.')
