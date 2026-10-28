"""Conservative PostgreSQL AST policy backed by database privileges/READ ONLY."""
from __future__ import annotations

from pglast import ast, parse_sql
from pglast.parser import ParseError

from src.storage import StoreError

FUNCTIONS = {'count','sum','avg','min','max','stddev','stddev_samp','stddev_pop','variance','var_samp','var_pop',
    'round','abs','ceil','ceiling','floor','trunc','power','sqrt','mod','nullif','coalesce','greatest','least',
    'date_trunc','date_part','extract','age','to_char','to_date','to_timestamp','make_date',
    'lower','upper','trim','btrim','ltrim','rtrim','length','char_length','substring','substr','replace','split_part',
    'concat','concat_ws','left','right','strpos','position','regexp_replace','regexp_match',
    'row_number','rank','dense_rank','percent_rank','cume_dist','ntile','lag','lead','first_value','last_value','nth_value',
    'percentile_cont','percentile_disc','corr','regr_slope','regr_intercept','regr_r2',
    'array_agg','string_agg','bool_and','bool_or','every','json_agg','jsonb_agg','json_build_object','jsonb_build_object',
    'json_object_agg','jsonb_object_agg','array_length','cardinality'}
TYPES={'text','varchar','bpchar','int2','int4','int8','integer','bigint','smallint','numeric','decimal','float4','float8',
       'real','double precision','bool','boolean','date','time','timestamp','timestamptz','interval','uuid','json','jsonb'}


def walk(node):
    # pglast nodes are iterable over attributes: https://pglast.readthedocs.io/en/latest/usage.html
    pending=[node]
    while pending:
        current=pending.pop()
        if isinstance(current,ast.Node):
            yield current
            pending.extend(getattr(current,attr) for attr in current)
        elif isinstance(current,(tuple,list)):
            pending.extend(current)


def validate_sql(sql: str, allowed_tables: set[str], *, params_count=0) -> dict:
    if not isinstance(sql,str) or not sql.strip() or len(sql.encode())>100000:
        raise StoreError('SQL 必须非空且不超过 100 KB。',422,'invalid_sql')
    try:
        roots=parse_sql(sql)
    except (ParseError,ValueError,RecursionError):
        raise StoreError('SQL 无法解析；使用一条 PostgreSQL SELECT 和 $1、$2 参数。',422,'invalid_sql') from None
    if len(roots)!=1 or not isinstance(roots[0].stmt,ast.SelectStmt):
        raise StoreError('只允许一条只读 SELECT 查询。',422,'sql_not_readonly')
    nodes=list(walk(roots[0].stmt))
    if len(nodes)>10000:
        raise StoreError('SQL语法树过大，请拆为同一批次内的相关查询。',422,'invalid_sql')
    tables=set();parameters=set()
    def relations(node,visible=frozenset()):
        if isinstance(node,ast.SelectStmt):
            local=set(visible)
            if node.withClause:
                if node.withClause.recursive:
                    raise StoreError('递归 CTE 不在当前只读查询范围。',403,'sql_recursive_not_allowed')
                for cte in node.withClause.ctes:
                    relations(cte.ctequery,frozenset(local))
                    local.add(cte.ctename)
            for attribute in node:
                if attribute!='withClause':
                    relations(getattr(node,attribute),frozenset(local))
        elif isinstance(node,ast.RangeVar):
            if node.catalogname or (node.schemaname and node.schemaname!='olist'):
                raise StoreError('查询引用了未授权的数据库或 schema。',403,'sql_object_not_allowed')
            if not node.schemaname and node.relname in visible:
                return
            relation='olist.'+node.relname
            if node.schemaname!='olist' or relation not in allowed_tables:
                raise StoreError(f'真实表必须显式使用获准的 olist.schema 对象：{relation}',403,'sql_object_not_allowed')
            tables.add(relation)
        elif isinstance(node,ast.Node):
            for attribute in node:
                relations(getattr(node,attribute),visible)
        elif isinstance(node,(tuple,list)):
            for child in node:
                relations(child,visible)
    try:
        relations(roots[0].stmt)
    except RecursionError:
        raise StoreError('SQL嵌套层级过深，请简化子查询结构。',422,'invalid_sql') from None
    for node in nodes:
        name=type(node).__name__
        if name.endswith('Stmt') and name!='SelectStmt':
            raise StoreError('CTE 和子查询同样只允许 SELECT。',422,'sql_not_readonly')
        if isinstance(node,ast.SelectStmt) and (node.intoClause or node.lockingClause):
            raise StoreError('不允许 SELECT INTO 或行锁查询。',422,'sql_not_readonly')
        if isinstance(node,ast.FuncCall):
            parts=[p.sval for p in node.funcname]
            if len(parts)>2 or (len(parts)==2 and parts[0]!='pg_catalog') or parts[-1] not in FUNCTIONS:
                raise StoreError('SQL 使用了未列入允许清单的函数。',403,'sql_function_not_allowed')
        if isinstance(node,ast.TypeCast):
            names=[p.sval for p in node.typeName.names]
            if len(names)>2 or (len(names)==2 and names[0]!='pg_catalog') or names[-1] not in TYPES:
                raise StoreError('SQL 使用了未允许的类型转换。',403,'sql_type_not_allowed')
        if isinstance(node,ast.A_Expr) and node.name and len(node.name)>1:
            raise StoreError('不允许自定义 schema 的运算符。',403,'sql_function_not_allowed')
        if isinstance(node,ast.CollateClause):
            raise StoreError('不允许自定义排序规则。',403,'sql_function_not_allowed')
        if isinstance(node,ast.ParamRef):
            parameters.add(node.number)
    if parameters and parameters!=set(range(1,params_count+1)):
        raise StoreError('SQL 参数数量不匹配；使用 $1、$2 与对应 params 数组。',422,'sql_parameter_mismatch')
    if not parameters and params_count:
        raise StoreError('查询未使用传入的 params。',422,'sql_parameter_mismatch')
    return {'tables':sorted(tables),'parameters':sorted(parameters)}
