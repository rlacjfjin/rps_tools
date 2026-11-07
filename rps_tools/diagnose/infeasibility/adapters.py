"""Strict, standard-library adapters for explicitly selected saved artifacts."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS


class Records(list):
    def __init__(self, records=()):
        super().__init__(records)
        self.by_code = {r.code: r for r in self}
        if len(self.by_code) != len(self):
            raise ValueError('模型中存在重复 code，不能可靠关联证据')
        self.index = self.by_code.keys()
        self.loc = self.by_code

    def get_variable(self, code):
        return self.by_code[code]


KINDS = {0: 'N', 1: 'E', 2: 'L', 3: 'G', 4: 'X', 5: 'FX', 6: 'LB', 7: 'UB'}


def proto_from_dict(data):
    """No evaluation, bound repair, start fallback or pandas dependency."""
    variables, rows = [], []
    if 'bounds' in data and 'rows' in data:
        vs = [dict(meta, code=code) for code, meta in data['bounds'].items()]
        cs = data['rows']
    else:
        vs = data.get('variable')
        cs = data.get('constarint', data.get('constraint'))
    if not isinstance(vs, list) or not isinstance(cs, list):
        raise ValueError('模型文件需要 bounds/rows 或 variable/constarint；不能使用简化表达式文本代替')
    for raw in vs:
        if not isinstance(raw, dict) or not isinstance(raw.get('code'), str):
            raise ValueError('变量缺少字符串 code')
        fields = dict(lb=None, ub=None, value=None, reduced_cost=None)
        fields.update(copy.deepcopy(raw))
        variables.append(NS(**fields))
    for raw in cs:
        kind = raw.get('type', raw.get('con_type'))
        kind = KINDS.get(kind, kind) if isinstance(kind, (int, str)) else None
        if kind not in KINDS.values() or not isinstance(raw.get('code'), str):
            raise ValueError('约束类型或 code 无效，不能猜测其数学含义')
        terms = raw.get('vars')
        if not isinstance(terms, list) or any(not isinstance(t, (list, tuple)) or not t for t in terms):
            raise ValueError('约束 vars 格式无效')
        fields = dict(RHS=raw.get('rhs', 0), dual_value=None, status=1)
        fields.update(copy.deepcopy(raw))
        fields['con_type'] = NS(name=kind)
        rows.append(NS(**fields))
    v, c = Records(variables), Records(rows)
    return NS(variable=v, constraint=c, name=data.get('name', ''),
              get_variable=v.get_variable, has_variable=lambda code, warning=False: code in v.by_code)


def model_from_dict(data, output, input_data):
    proto = proto_from_dict(data)
    suffix = str(input_data.get('plant') or '') + str(input_data.get('period') or '')
    groups = {}
    for table, attr in (('materialList', 'material_table'), ('crude', 'crude_table')):
        originals = {m['code']: m for m in input_data.get(table, []) or []}
        materials = []
        for m in output.get(table, []) or []:
            original = originals.get(m['code'], {})
            # A missing basis cannot safely be guessed to mean a regular Q variable.
            basis = m.get('basi', original.get('basi'))
            if basis is None:
                continue
            materials.append(NS(_code=m['code'], code=m['code']+suffix,
                                name=m.get('name') or m['code'], basi=NS(name=basis),
                                input=m.get('input', original.get('input', [])),
                                output=m.get('output', original.get('output', []))))
        groups['material_list' if table == 'materialList' else 'crude_table'] = NS(**{attr: materials})
    return NS(mpproto=proto, **groups)


def read_json(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('gb18030')
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('诊断文件顶层必须为 JSON 对象')
    return data


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()


def load_artifacts(params):
    """Never follow paths inside uploaded JSON, nor choose files by mtime."""
    roles = ('input', 'output', 'snapshot', 'model', 'native', 'trace', 'row_map')
    data, provenance, ids = {}, {}, set()
    for role in roles:
        path = params.get(role)
        if not path:
            continue
        provenance[role] = {'sha256': digest(path)}
        if role == 'trace':
            data[role] = str(path)
            continue
        item = read_json(path)
        metadata = item.get('artifact_metadata') or {}
        for run_id in (item.get('request_id'), item.get('run_id'), metadata.get('run_id')):
            if run_id is not None:
                ids.add(str(run_id))
        data[role] = item
    if len(ids) > 1:
        raise ValueError('文件属于不同 request_id/run_id，已停止跨运行归因')
    matched = []
    if params.get('manifest'):
        manifest = read_json(params['manifest'])
        expected = manifest.get('files')
        if not isinstance(expected, dict):
            raise ValueError('manifest.files 必须按 input/output/model 等角色列出 sha256')
        if manifest.get('run_id') is not None and ids and str(manifest['run_id']) not in ids:
            raise ValueError('manifest.run_id 与文件不一致')
        for role, meta in provenance.items():
            wanted = (expected.get(role) or {}).get('sha256')
            if not wanted or str(wanted).lower() != meta['sha256']:
                raise ValueError(f'manifest 中 {role} 的 SHA256 缺失或不一致')
            matched.append(role)
    warnings = []
    if len(provenance) > 1 and not matched:
        warnings.append('文件由用户显式配对；未提供完整哈希清单，同次运行关联未完全验证。')
    return data, {'files': provenance, 'association': 'manifest_hashes' if matched else 'explicit_files',
                  'run_ids': sorted(ids)}, warnings
