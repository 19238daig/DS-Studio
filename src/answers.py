"""Small, evidence-bound answers for chat, independent of report presentation.

The application selects/render metrics rather than dumping an analysis object.
Free-form labels still require model judgement; numbers always come from the
authorized analysis, and the only derived comparison here is Decimal ordering.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.schemas import ReportMetricReference
from src.storage import StoreError


TOKEN = re.compile(r'\{\{metric:([a-zA-Z0-9_-]+):([^{}:]+)\}\}')
NUMBER = re.compile(r'\d|百分之|[零〇一二两三四五六七八九十百千万亿]{2,}|'
                    r'[零〇一二两三四五六七八九十百千万亿]+点[零〇一二两三四五六七八九十百千万亿]+|'
                    r'[零〇一二两三四五六七八九十百千万亿](?:倍|成|个|项|行|列|分|元|[%％])|'
                    r'(?:为|是|等于|约|达到|超过|低于|高于|至少|最多|合计|共|均值|平均值|总额|数量|占比)'
                    r'\s*[零〇一二两三四五六七八九十百千万亿](?=$|[。；，、%％\s）)])')
# Number-shaped tokens (superset of NUMBER) used to tell identifiers from
# claims: "2021年"/"第1组" name real data groups, while "999"/"为三" assert a
# statistic. Time-unit tails (月日时分) stay out: "3个月"/"12月" usually states
# a duration or count, which must stay bound.
NUMERIC_TOKEN = re.compile(
    r'\d+(?:[.,/－—~～-]\d+)*[%％]?[年季度期组项名号批]?'
    r'|百分之[零〇一二两三四五六七八九十百千万亿]+'
    r'|负?[零〇一二两三四五六七八九十百千万亿]{2,}'
    r'|[零〇一二两三四五六七八九十百千万亿]+点[零〇一二两三四五六七八九十百千万亿]+'
    r'|[零〇一二两三四五六七八九十百千万亿](?:倍|成|个|项|行|列|分|元|[%％])'
    r'|负?[零〇一二两三四五六七八九十百千万亿](?=$|[。；，、%％\s）)])'
    r'|(?:为|是|等于|约|达到|超过|低于|高于|至少|最多|合计|共|均值|平均值|总额|数量|占比)'
    r'\s*[零〇一二两三四五六七八九十百千万亿](?=$|[。；，、%％\s）)])')
NUMERIC_TAIL = '年季度期组项名号批%％'
DEFINITION_ID_KEYS = re.compile(r'(?:^|_)ids?$', re.I)
META = {'source_rows', 'result_rows', 'source_characters', 'excerpt_characters'}
OPERATIONS = {
    'mean': r'平均|均值|\b(?:mean|avg|average)\b',
    'median': r'中位数|\bmedian\b',
    'std': r'标准差|\b(?:std|stdev)\b',
    'min': r'最小|最低|\b(?:min|minimum)\b',
    'max': r'最大|最高|\b(?:max|maximum)\b',
    'sum': r'合计|总额|总和|求和|\bsum\b',
    'count': r'数量|总数|行数|记录数|几行|多少行|\bcount\b|\bhow\s+many\s+rows\b|\bnumber\s+of\s+(?:rows|records)\b',
    'nunique': r'去重|不重复|独立.*(?:数|多少)|\b(?:nunique|unique)\b',
}
OP_LABELS = {'mean': '平均值', 'median': '中位数', 'std': '标准差', 'min': '最小值',
             'max': '最大值', 'sum': '合计', 'count': '数量', 'nunique': '非重复值数量'}
NEGATED = re.compile(r'(?:不需要|不必|无需|无须|不要|不用|别|不显示|不返回)|'
                     r"\b(?:without|not|no|don['’]t)\b", re.I)


def positive_clauses(request: str) -> str:
    return '，'.join(clause for clause in re.split(r'[，,。.!！?？;；\n]|而是|但是|只需|只要', request)
                    if not NEGATED.search(clause))


def requested_operations(request: str) -> set[str]:
    text = positive_clauses(request)
    if re.search(r'描述统计|全部统计|所有统计|完整.*统计|\bdescribe\b', text, re.I):
        return set(OPERATIONS)
    wanted = {key for key, pattern in OPERATIONS.items() if re.search(pattern, text, re.I)}
    if 'nunique' in wanted and not re.search(r'总数|行数|记录数|\bcount\b', text, re.I):
        wanted.discard('count')
    return wanted


def wants_tables(request: str) -> bool:
    text = positive_clauses(request)
    explicit = bool(re.search(r'明细|原始数据|逐行|表格|完整结果表|描述统计|列出.*(?:行|记录)|给我.*(?:数据行|记录)|排名|Top\s*\d+|前\s*\d+\s*(?:名|个|项)|'
                              r'\b(?:table|raw\s+data)\b', text, re.I))
    show_rows = (bool(re.search(r'\b(?:show|list|display|return)\b.{0,25}\b(?:rows|records)\b', text, re.I)) and
                 not re.search(r'\b(?:count|number|many)\b', text, re.I))
    return explicit or show_rows


def answer_table_limit(request: str) -> int:
    match = re.search(r'(?:Top|前)\s*(\d+)', positive_clauses(request), re.I)
    return max(1, min(int(match[1]), 100)) if match else 20


def wants_method(request: str) -> bool:
    return bool(re.search(r'过程|步骤|口径|怎么算|如何计算|计算方法|SQL语句|'
                          r'(?:给|看|展示|输出|解释|附|提供|显示|查看).{0,10}(?:SQL|代码)|'
                          r'\b(?:steps|method)\b|\b(?:show|explain|provide)\b.{0,20}\b(?:sql|code|calculation)\b',
                          positive_clauses(request), re.I))


def metric_operation(key: str) -> str | None:
    tokens = re.split(r'[_\s.-]+', key.lower())
    for op, aliases in {'mean': {'mean', 'avg', 'average'}, 'median': {'median'},
                        'std': {'std', 'stdev'}, 'min': {'min', 'minimum'},
                        'max': {'max', 'maximum'}, 'sum': {'sum'},
                        'count': {'count'}, 'nunique': {'nunique', 'unique'}}.items():
        if any(token in aliases for token in tokens):
            return op
    return None


def _mentioned(name: str, text: str) -> bool:
    if re.fullmatch(r'[\w .-]+', name, re.ASCII):
        return bool(re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', text, re.I))
    return name.lower() in text.lower()


def relevant_metric(analysis, key: str, request: str) -> bool:
    if key in META:
        text = positive_clauses(request)
        result_count = bool(re.search(r'结果.*(?:行数|几行|多少行)|\bresult\s*rows?\b', text, re.I))
        return (key == 'source_rows' and not result_count and bool(re.search(r'行数|几行|多少行|记录数|\b(?:row\s*count|count(?:\s+the)?\s+rows|how\s+many\s+rows|number\s+of\s+(?:rows|records))\b', text, re.I)) or
                key == 'result_rows' and result_count or
                key in {'source_characters', 'excerpt_characters'} and '字符' in positive_clauses(request))
    wanted = requested_operations(request)
    operation = metric_operation(key)
    if wanted and operation and operation not in wanted:
        return False
    # Fixed aggregations tell us the original column names. If the user names
    # one, a sibling aggregation of another column is not part of this answer.
    definition = analysis.metric_definitions.get(key, {})
    aggregations = definition.get('aggregations', {}) if isinstance(definition, dict) else {}
    if isinstance(aggregations, dict):
        named = [column for column in aggregations if _mentioned(column, request)]
        if named and not any(key.lower().startswith(column.lower() + '_') for column in named):
            return False
    return True


def narrow_aggregations(request: str, aggregations: dict) -> dict:
    """Keep exact explicitly requested scalar operations, without guessing columns.

    Call only for plain chat aggregates. Reports/groupings and broad descriptive
    questions keep their original scope; metadata is still recorded internally.
    """
    wanted = requested_operations(request)
    if not wanted or re.search(r'描述统计|全部统计|所有统计|全面|完整.*统计|\bdescribe\b', request, re.I):
        return aggregations
    named = [column for column in aggregations if _mentioned(column, request)]
    result = {}
    for column, funcs in aggregations.items():
        if named and column not in named:
            continue
        keep = [func for func in ([funcs] if isinstance(funcs, str) else funcs) if func in wanted]
        if keep:
            result[column] = keep[0] if len(keep) == 1 else keep
    # If there is no matching function, return a corrective tool error rather
    # than silently calculating an unrelated statistic.
    if not result:
        raise StoreError('本次问题只需' + '、'.join(OP_LABELS[op] for op in sorted(wanted)) +
                         '，请仅使用对应聚合函数。', 422, 'unrequested_aggregation')
    return result


def exact(value) -> str:
    if value is None:
        return '暂无有效值'
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def markdown(value) -> str:
    return re.sub(r'([\\`*_{}\[\]()#+.!|>])', r'\\\1', str(value))


def _label(runtime, ref, duplicates=False) -> str:
    if ref.label:
        return ref.label.replace('\n', ' ')[:150]
    # This task-local map is written only by deterministic executors using
    # already-authorized file/catalog metadata, never by model tool arguments.
    # Numeric source/column names are identities, not free-form numeric claims.
    trusted = getattr(runtime, 'trusted_metric_labels', {}).get((ref.analysis_id, ref.metric_key))
    if trusted:
        return trusted.replace('\n', ' ')
    analysis = runtime.authorize_analysis(ref.analysis_id)
    label = ref.metric_key
    label = {'source_rows': '数据行数', 'result_rows': '结果行数',
             'source_characters': '原文字符数', 'excerpt_characters': '摘录字符数'}.get(label, label)
    for op, translated in OP_LABELS.items():
        if label.endswith('_' + op):
            label = label[:-len(op)-1] + translated
            break
    if duplicates and analysis.input_versions:
        names = [Path(runtime.store.get_file(fid, runtime.task.session_id)['name']).stem
                 for fid in analysis.input_versions]
        label = '、'.join(names) + '的' + label
    return label


def automatic_refs(runtime, ids) -> list[ReportMetricReference]:
    refs = []
    for aid in ids:
        analysis = runtime.authorize_analysis(aid)
        for key in analysis.metrics:
            if relevant_metric(analysis, key, runtime.task.request):
                refs.append(ReportMetricReference(analysis_id=aid, metric_key=key))
    # A narrow request with recognized operations can be served from matching
    # aggregates. Otherwise multiple unknown aliases need model selection.
    if len(refs) > 1:
        wanted = requested_operations(runtime.task.request)
        matching = [ref for ref in refs if ref.metric_key in META or metric_operation(ref.metric_key) in wanted]
        return matching if matching else []
    return refs


def _definition_texts(value, result: list[str]) -> None:
    if isinstance(value, str):
        result.append(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _definition_texts(item, result)
    elif isinstance(value, dict):
        for key, item in value.items():
            # Hex execution/artifact/file identifiers are not data identifiers;
            # their digit runs must never enter the trusted corpus.
            if isinstance(key, str) and DEFINITION_ID_KEYS.search(key):
                continue
            if isinstance(key, str):
                _definition_texts(key, result)
            _definition_texts(item, result)


def identifier_tokens(analyses, extra_texts=()) -> set[str]:
    """Numeric tokens that name registered results, not free-form claims.

    Sources are task-registered metadata only: metric keys, metric definitions
    (column names, group/filter values and the analyst's written definitions)
    plus executor-written labels and input file names passed as extra_texts.
    Registered metric VALUES stay out so a statistic can never be typed
    unbound just because it was also computed.
    """
    texts: list[str] = []
    for analysis in analyses:
        texts.extend(str(key) for key in getattr(analysis, 'metrics', {}) or {})
        for definition in (getattr(analysis, 'metric_definitions', {}) or {}).values():
            _definition_texts(definition, texts)
    texts.extend(str(text) for text in extra_texts)
    trusted = set()
    for text in texts:
        for token in NUMERIC_TOKEN.findall(text):
            trusted.add(token)
            trusted.add(token.rstrip(NUMERIC_TAIL))
    return {token for token in trusted if token}


def runtime_identifier_tokens(runtime) -> set[str]:
    store = getattr(runtime, 'store', None)
    ids = list(dict.fromkeys(list(getattr(runtime, 'allowed_analysis_ids', []) or []) +
                             list(getattr(runtime, 'analyses', []) or [])))
    analyses = []
    for aid in ids:
        try:
            analyses.append(runtime.authorize_analysis(aid))
        except StoreError:
            if store is not None:
                try:
                    analyses.append(store.get_analysis(aid, runtime.task.session_id))
                except StoreError:
                    continue
    extra = list(getattr(runtime, 'trusted_metric_labels', {}).values())
    if store is not None:
        for analysis in analyses:
            for fid in getattr(analysis, 'input_versions', {}) or {}:
                try:
                    extra.append(Path(store.get_file(fid, runtime.task.session_id)['name']).stem)
                except StoreError:
                    continue
    return identifier_tokens(analyses, extra)


def unbound_numbers(text: str, trusted: set[str]) -> list[str]:
    offenders = []
    for token in NUMERIC_TOKEN.findall(text):
        if token in trusted or token.rstrip(NUMERIC_TAIL) in trusted:
            continue
        if token not in offenders:
            offenders.append(token)
    return offenders


def validate_refs(runtime, refs) -> list[ReportMetricReference]:
    result = []
    for ref in refs:
        if ref.analysis_id not in runtime.allowed_analysis_ids:
            raise StoreError('Answer analysis is not authorized', 422, 'analysis_not_authorized')
        analysis = runtime.authorize_analysis(ref.analysis_id)
        if ref.metric_key not in analysis.metrics:
            raise StoreError('Answer metric does not exist', 422, 'missing_metric')
        if not relevant_metric(analysis, ref.metric_key, runtime.task.request):
            continue
        if ref.label and ref.label != ref.metric_key and NUMBER.search(ref.label):
            # Labels stay qualitative even for data identifiers: the app renders
            # years/group names from metric keys or executor-written labels, so
            # a numeric label can only add an unverified claim.
            raise StoreError('指标标签只能写定性名称，不能填入数值；'
                             '年份、分组名等标识请省略label由应用渲染。', 422, 'invalid_answer_label')
        if (ref.analysis_id, ref.metric_key) not in {(item.analysis_id, item.metric_key) for item in result}:
            result.append(ref)
    return result


def bound_template(runtime, text: str) -> tuple[str | None, list[ReportMetricReference]]:
    if '{{metric:' not in text:
        return None, []
    residue = TOKEN.sub('', text)
    if '{{metric:' in residue:
        raise StoreError('请使用{{metric:analysis_id:metric_key}}绑定回答数值。', 422, 'invalid_answer_template')
    offenders = unbound_numbers(residue, runtime_identifier_tokens(runtime))
    if offenders:
        raise StoreError('回答含未绑定数值：' + '、'.join(offenders[:6]) +
                         '；统计数值必须改用{{metric:analysis_id:metric_key}}占位符，'
                         '数据中真实存在的年份、分组名、列名等标识可直接书写，不要重新计算。',
                         422, 'unbound_answer')
    refs = [ReportMetricReference(analysis_id=match[1], metric_key=match[2]) for match in TOKEN.finditer(text)]
    selected = validate_refs(runtime, refs)
    if {(r.analysis_id, r.metric_key) for r in refs} != {(r.analysis_id, r.metric_key) for r in selected}:
        raise StoreError('回答引用了本次问题无关的统计或内部元数据，请仅保留直接结论。', 422, 'unrelated_answer_metric')
    def replace(match):
        value = runtime.authorize_analysis(match[1]).metrics[match[2]]
        try:
            return exact(value) if Decimal(str(value)).is_finite() else markdown(exact(value))
        except (InvalidOperation, ValueError):
            return markdown(exact(value))
    return TOKEN.sub(replace, text).strip(), selected


def _comparable(runtime, left, right) -> bool:
    a, b = [runtime.authorize_analysis(ref.analysis_id) for ref in (left, right)]
    da, db = [analysis.metric_definitions.get(ref.metric_key, {}) for analysis, ref in ((a, left), (b, right))]
    if isinstance(da, dict) and isinstance(db, dict) and da.get('unit') != db.get('unit'):
        return False
    if left.metric_key == right.metric_key:
        return True
    # Require a common actual measure; sharing only "avg" or "mean" is not
    # enough to compare currency with latency, or count with mean.
    if metric_operation(left.metric_key) != metric_operation(right.metric_key):
        return False
    stop = {'mean', 'avg', 'average', 'median', 'std', 'sum', 'total', 'count', 'min', 'max',
            'customer', 'customers', 'user', 'users', 'repeat', 'single', 'new', 'old', 'a', 'b'}
    tokens = [set(re.split(r'[_\W]+', ref.metric_key.lower())) - stop for ref in (left, right)]
    return bool((tokens[0] & tokens[1]) - {''})


def render_metrics(runtime, refs) -> str:
    duplicates = len({ref.metric_key for ref in refs}) < len(refs)
    labels = [_label(runtime, ref, duplicates) for ref in refs]
    values = [runtime.authorize_analysis(ref.analysis_id).metrics[ref.metric_key] for ref in refs]
    if (len(refs) == 2 and re.search(r'对比|比较|哪(?:一)?(?:个|种)|谁.*(?:高|低|多|少)|更高|更低|高低|差异|\bcompar', runtime.task.request, re.I) and
            _comparable(runtime, *refs)):
        try:
            left, right = (Decimal(str(value)) for value in values)
            if left.is_finite() and right.is_finite():
                relation = '高于' if left > right else '低于' if left < right else '等于'
                return f'{markdown(labels[0])}（**{exact(values[0])}**）{relation}{markdown(labels[1])}（**{exact(values[1])}**）。'
        except (InvalidOperation, ValueError):
            pass
    def display(value):
        try:
            return exact(value) if Decimal(str(value)).is_finite() else markdown(exact(value))
        except (InvalidOperation, ValueError):
            return markdown(exact(value))
    return '\n\n'.join(f'{markdown(label)}：**{display(value)}**。' for label, value in zip(labels, values))
