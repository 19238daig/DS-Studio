"""Create PostgreSQL connections inside each task process, never in the LLM."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager

import psycopg
from psycopg import IsolationLevel

from src.storage import StoreError

TABLES = ('customers','orders','order_items','order_payments','order_reviews','products','sellers')
VIEWS = ('v_order_items_agg','v_order_payments_agg','v_order_reviews_agg','v_order_summary')
SOURCES = {
    'olist': {'name':'Olist 电商分析', 'tables': TABLES + VIEWS, 'restricted':False},
    'sellers_restricted': {'name':'Olist 卖家（受限城市）', 'tables':('sellers',), 'restricted':True},
}


def source_config(settings, source_id):
    if not settings.db_enabled:
        raise StoreError('数据库功能未启用；请配置 DB_ENABLED 与数据库连接。',503,'database_disabled')
    if source_id not in SOURCES:
        raise StoreError('未配置的数据源。',404,'database_source_not_found')
    profile = SOURCES[source_id]
    user = settings.db_restricted_user if profile['restricted'] else settings.db_user
    password = settings.db_restricted_password if profile['restricted'] else settings.db_password
    if not user or not password.get_secret_value():
        raise StoreError('数据库账号尚未配置，请在本机 .env 填写对应 DB_USER / DB_PASSWORD。',503,'database_not_configured')
    return profile, user, password.get_secret_value()


def safe_error(exc, settings):
    # libpq errors can contain connection details and localized undecodable text.
    # Do not expose raw driver messages, DSNs, passwords or SQL parameter values.
    state = getattr(exc, 'sqlstate', None)
    messages = {'57014':'SQL 执行已取消或超过单条查询时限。','42501':'数据库拒绝访问，当前账号没有所需权限。',
                '42P01':'查询表不存在或当前账号不可见。','42703':'查询字段不存在。',
                '42601':'SQL 语法错误，请按 PostgreSQL 语法修正。','22012':'SQL 计算出现除零。',
                '42883':'函数或运算符与字段类型不匹配，请核对字段并显式转换类型。',
                '42803':'聚合与分组字段不一致，请核对 GROUP BY。','42702':'字段名称存在歧义，请增加表别名。',
                '42804':'SQL表达式的数据类型不匹配。','22003':'数值超过目标类型范围，请使用 NUMERIC。',
                '22P02':'参数或字段无法转换为指定类型。','22007':'日期或时间格式无效。',
                '28P01':'数据库密码认证失败，请检查本机连接配置。'}
    return StoreError(messages.get(state,'PostgreSQL 连接或执行失败，请核对本机连接与数据库日志。') +
                      (f' SQLSTATE={state}' if state else ''),503 if not state else 422,'database_error')


@contextmanager
def connect(settings, source_id, *, admin=False):
    if admin:
        user,password=settings.db_admin_user,settings.db_admin_password.get_secret_value()
        if not user or not password:
            raise StoreError('管理命令需要本机 DB_ADMIN_USER / DB_ADMIN_PASSWORD。',503,'database_admin_not_configured')
    else:
        _,user,password=source_config(settings,source_id)
    try:
        # Official connection/transaction APIs: https://www.psycopg.org/psycopg3/docs/basic/transactions.html
        connection=psycopg.connect(host=settings.db_host,port=settings.db_port,dbname=settings.db_name,
            user=user,password=password,connect_timeout=settings.db_connect_timeout_seconds,
            application_name='ds_project_admin' if admin else 'ds_project_readonly',autocommit=True)
    except psycopg.Error as exc:
        raise safe_error(exc,settings) from None
    try:
        if not admin:
            connection.read_only=True
            connection.isolation_level=IsolationLevel.REPEATABLE_READ
        yield connection
    except psycopg.Error as exc:
        raise safe_error(exc,settings) from None
    finally:
        connection.close()


def identity(connection, source_id):
    # Deparsed policy/view definitions depend on search_path. Normalize it in
    # both standalone catalog calls and query transactions before hashing.
    connection.execute("SELECT pg_catalog.set_config('search_path','pg_catalog, olist',true)")
    profile=SOURCES[source_id]
    user,db,superuser,create_role,create_db,bypass,replication=connection.execute('''
        SELECT current_user,current_database(),rolsuper,rolcreaterole,rolcreatedb,rolbypassrls,rolreplication
        FROM pg_catalog.pg_roles WHERE rolname=current_user''').fetchone()
    if any((superuser,create_role,create_db,bypass,replication)):
        raise StoreError('应用查询账号权限过高，必须使用非超级用户、无角色管理和无 BYPASSRLS 的只读身份。',403,'unsafe_database_role')
    schema=connection.execute("SELECT oid,nspacl::text,pg_catalog.has_schema_privilege(oid,'USAGE') FROM pg_catalog.pg_namespace WHERE nspname='olist'").fetchone()
    if not schema or not schema[2]:
        raise StoreError('当前账号没有 Olist schema 的读取权限。',403,'database_schema_not_allowed')
    if connection.execute("SELECT pg_catalog.has_schema_privilege('olist','CREATE')").fetchone()[0]:
        raise StoreError('应用查询身份具有业务schema的创建权限，请配置独立只读账号。',403,'unsafe_database_role')
    owned=connection.execute('''SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='olist' AND c.relname=ANY(%s) AND pg_catalog.pg_has_role(c.relowner,'USAGE')''',(list(profile['tables']),)).fetchall()
    if owned:
        raise StoreError('查询账号拥有业务表或继承其所有者身份，不能作为 Agent 只读账号。',403,'unsafe_database_role')
    relations=connection.execute('''SELECT n.nspname,c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,c.relacl::text,c.reloptions
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='olist' AND c.relkind IN ('r','v') AND c.relname=ANY(%s)
        AND pg_catalog.has_table_privilege(c.oid,'SELECT') ORDER BY c.relname''',(list(profile['tables']),)).fetchall()
    # Views must retain caller permissions and RLS; ordinary owner-based views are excluded.
    relations=[r for r in relations if r[2]!='v' or 'security_invoker=true' in (r[6] or [])]
    writes=connection.execute('''SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='olist' AND c.relname=ANY(%s)
        AND (pg_catalog.has_table_privilege(c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
             OR pg_catalog.has_any_column_privilege(c.oid,'INSERT,UPDATE,REFERENCES'))''',(list(TABLES+VIEWS),)).fetchall()
    if writes:
        raise StoreError('应用查询身份具有业务表写入权限，请配置独立只读账号。',403,'unsafe_database_role')
    if profile['restricted']:
        # A mistaken full-reader configuration must never silently turn this
        # explicitly city-limited source into a full sellers source.
        other_access=connection.execute('''SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='olist' AND c.relname=ANY(%s) AND pg_catalog.has_table_privilege(c.oid,'SELECT')''',([name for name in TABLES+VIEWS if name!='sellers'],)).fetchall()
        sellers=next((r for r in relations if r[1]=='sellers'),None)
        if other_access or not sellers or not sellers[3]:
            raise StoreError('受限数据源账号范围过宽或 sellers 未启用RLS。',403,'unsafe_database_role')
        if connection.execute("SELECT EXISTS(SELECT 1 FROM olist.sellers WHERE seller_city IS DISTINCT FROM 'santo andre')").fetchone()[0]:
            raise StoreError('受限数据源返回了允许城市之外的卖家，请修正RLS配置。',403,'unsafe_database_role')
    policies=connection.execute('''SELECT schemaname,tablename,policyname,permissive,roles::text,cmd,qual,with_check
        FROM pg_catalog.pg_policies WHERE schemaname='olist' AND tablename=ANY(%s)
        ORDER BY tablename,policyname''',(list(profile['tables']),)).fetchall()
    memberships=connection.execute('''SELECT r.rolname FROM pg_catalog.pg_roles r
        WHERE pg_catalog.pg_has_role(r.oid,'USAGE') ORDER BY r.rolname''').fetchall()
    definitions=connection.execute('''SELECT c.relname,a.attnum,a.attname,a.atttypid,a.atttypmod,a.attnotnull,a.attacl::text,
            CASE WHEN c.relkind='v' THEN pg_catalog.pg_get_viewdef(c.oid,true) ELSE NULL END
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='olist' AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum''',(list(profile['tables']),)).fetchall()
    # Include concrete identity, relation ACLs, policy definitions and inherited roles.
    database_oid=connection.execute('SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database()').fetchone()[0]
    payload={'source_id':source_id,'user':user,'database':db,'database_oid':database_oid,
             'host':connection.info.host,'port':connection.info.port,
             'schema':schema,'relations':relations,'policies':policies,'memberships':memberships,'definitions':definitions}
    scope=hashlib.sha256(json.dumps(payload,default=str,sort_keys=True).encode()).hexdigest()
    return {'source_id':source_id,'user':user,'database':db,'scope_fingerprint':scope,'relations':relations,
            'restricted':profile['restricted'],'scope_description':"仅当前账号获准的 sellers 行；不开放订单表" if profile['restricted'] else '当前只读账号获准的 Olist 表与调用者权限视图'}
