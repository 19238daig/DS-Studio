"""Only actual-role-visible, configured Olist metadata is presented to Agents."""
from src.database.connection import SOURCES, connect, identity
from src.storage import StoreError

DESCRIPTIONS={
 'customers':'订单客户档案；customer_id关联订单，customer_unique_id用于独立客户去重和复购。',
 'orders':'每订单一行；必须明确全部/已交付/取消等订单状态与日期范围。',
 'order_items':'每订单商品明细一行；order_item_id是序号，不能求和当数量。price为商品金额，freight_value为运费。',
 'order_payments':'每订单可多笔支付；payment_value是支付金额，不等同商品金额。',
 'order_reviews':'每订单可多条评价，键为(review_id,order_id)。',
 'products':'商品资料；源文件lenght拼写已映射为length。',
 'sellers':'卖家资料；受限身份只可见允许城市，不代表整个订单表自动被城市隔离。',
 'v_order_items_agg':'每订单一行的商品金额、运费和商品明细数量。',
 'v_order_payments_agg':'每订单一行的支付金额及支付记录数量。',
 'v_order_reviews_agg':'每订单一行的评价数和平均评分。',
 'v_order_summary':'每订单一行的综合分析；先各子表聚合再关联，避免金额重复累计。'}


def source_list(settings):
    if not settings.db_enabled:
        return []
    return [dict(source_id=key,name=value['name'],kind='postgresql',schema='olist',readonly=True,
        configured=bool((settings.db_restricted_password if value['restricted'] else settings.db_password).get_secret_value()),
        restricted=value['restricted']) for key,value in SOURCES.items()]


def describe(connection,source_id):
    scope=identity(connection,source_id)
    relations=scope.pop('relations')
    # Fetch all columns/constraints in two ordered queries instead of two
    # per relation. The identity fingerprint still uses identity() unchanged.
    # pg_catalog APIs: https://www.postgresql.org/docs/current/catalogs.html
    names=[row[1] for row in relations]
    columns_by_name={name:[] for name in names}
    constraints_by_name={name:[] for name in names}
    if names:
        for name,*column in connection.execute('''SELECT c.relname,a.attname,
                pg_catalog.format_type(a.atttypid,a.atttypmod),a.attnotnull,
                pg_catalog.col_description(a.attrelid,a.attnum)
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='olist' AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY c.relname,a.attnum''',(names,)).fetchall():
            columns_by_name[name].append(column)
        for name,*constraint in connection.execute('''SELECT c.relname,con.contype,
                pg_catalog.pg_get_constraintdef(con.oid)
            FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class c ON c.oid=con.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='olist' AND c.relname=ANY(%s) AND con.contype IN ('p','f')
            ORDER BY c.relname,con.conname''',(names,)).fetchall():
            constraints_by_name[name].append(constraint)
    tables=[]
    for schema,name,kind,rls,forced,acl,options in relations:
        columns=columns_by_name[name]
        constraints=constraints_by_name[name]
        tables.append(dict(schema=schema,name=name,qualified_name=f'{schema}.{name}',kind='view' if kind=='v' else 'table',
            description=DESCRIPTIONS.get(name,''),rls_enabled=rls,rls_forced=forced,
            columns=[dict(name=x[0],data_type=x[1],nullable=not x[2],description=x[3] or '') for x in columns],
            primary_key=[x[1] for x in constraints if x[0]=='p'],foreign_keys=[x[1] for x in constraints if x[0]=='f']))
    return {**scope,'tables':tables,'notes':['金额使用完整 NUMERIC 精度；预览不能代替完整统计。',
        '订单各子表先聚合后连接，禁止用SUM(DISTINCT金额)掩盖错误连接。','不带时区的源时间不自动转换时区；币种以业务确认口径为准。']}


def catalog(settings,source_id):
    with connect(settings,source_id) as c:
        with c.transaction():
            return describe(c,source_id)
