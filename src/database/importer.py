"""Explicit local administration; no management capability is exposed to an Agent.

COPY is client-side STDIN: https://www.psycopg.org/psycopg3/docs/basic/copy.html
All seven tables and the batch ledger commit together; existing rows are never replaced.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from pathlib import Path

import psycopg
from filelock import FileLock
from psycopg import sql
from psycopg.types.json import Jsonb

from src.config import ROOT, Settings
from src.database.connection import TABLES, VIEWS, safe_error
from src.storage import StoreError

MARKER='ds_project:olist:v1'
OWNER='ds_olist_owner'
DEFINITIONS={
 'customers': [('customer_id','text'),('customer_unique_id','text'),('customer_zip_code_prefix','text'),('customer_city','text'),('customer_state','text')],
 'products': [('product_id','text'),('product_category_name','text'),('product_name_length','integer'),('product_description_length','integer'),('product_photos_qty','integer'),('product_weight_g','integer'),('product_length_cm','integer'),('product_height_cm','integer'),('product_width_cm','integer')],
 'sellers': [('seller_id','text'),('seller_zip_code_prefix','text'),('seller_city','text'),('seller_state','text')],
 'orders': [('order_id','text'),('customer_id','text'),('order_status','text'),('order_purchase_timestamp','timestamp'),('order_approved_at','timestamp'),('order_delivered_carrier_date','timestamp'),('order_delivered_customer_date','timestamp'),('order_estimated_delivery_date','timestamp')],
 'order_items': [('order_id','text'),('order_item_id','integer'),('product_id','text'),('seller_id','text'),('shipping_limit_date','timestamp'),('price','numeric(18,2)'),('freight_value','numeric(18,2)')],
 'order_payments': [('order_id','text'),('payment_sequential','integer'),('payment_type','text'),('payment_installments','integer'),('payment_value','numeric(18,2)')],
 'order_reviews': [('review_id','text'),('order_id','text'),('review_score','integer'),('review_comment_title','text'),('review_comment_message','text'),('review_creation_date','timestamp'),('review_answer_timestamp','timestamp')],
}
KEYS={'customers':['customer_id'],'products':['product_id'],'sellers':['seller_id'],'orders':['order_id'],
      'order_items':['order_id','order_item_id'],'order_payments':['order_id','payment_sequential'],'order_reviews':['review_id','order_id']}
FOREIGN_KEYS={'orders':[('customer_id','customers','customer_id')],
 'order_items':[('order_id','orders','order_id'),('product_id','products','product_id'),('seller_id','sellers','seller_id')],
 'order_payments':[('order_id','orders','order_id')],'order_reviews':[('order_id','orders','order_id')]}


@contextmanager
def admin_connect(settings, database=None):
    password=settings.db_admin_password.get_secret_value()
    if not settings.db_admin_user or not password:
        raise StoreError('本机管理配置缺少 DB_ADMIN_USER / DB_ADMIN_PASSWORD。',503,'database_admin_not_configured')
    conn=None
    try:
        conn=psycopg.connect(host=settings.db_host,port=settings.db_port,dbname=database or settings.db_name,
            user=settings.db_admin_user,password=password,autocommit=True,
            connect_timeout=settings.db_connect_timeout_seconds,application_name='ds_project_management')
        yield conn
    except psycopg.Error as exc:
        raise safe_error(exc,settings) from None
    finally:
        if conn is not None: conn.close()


def _env_update(path, values):
    """Read under a cross-process lock and replace only explicitly owned DB keys."""
    path=Path(path)
    with FileLock(str(path)+'.db.lock'):
        existing=path.read_text(encoding='utf-8') if path.exists() else ''
        lines=existing.splitlines()
        for key,value in values.items():
            replacement=f'{key}={json.dumps(str(value),ensure_ascii=False)}'
            matched=False
            for index,line in enumerate(lines):
                if re.match(r'^\s*(?:export\s+)?'+re.escape(key)+r'\s*=',line):
                    lines[index]=replacement; matched=True
            if not matched: lines.append(replacement)
        temporary=path.with_name(path.name+'.db.tmp')
        temporary.write_text('\n'.join(lines)+'\n',encoding='utf-8')
        temporary.replace(path)


def _role(conn,name,password=None):
    found=conn.execute('SELECT shobj_description(oid,\'pg_authid\'),rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,rolreplication,rolcanlogin FROM pg_roles WHERE rolname=%s',(name,)).fetchone()
    if found:
        if found[0]!=MARKER or any(found[1:6]) or bool(password)!=found[6]:
            raise StoreError(f'已有角色 {name} 并非本项目安全角色，拒绝覆盖。',409,'database_object_conflict')
        return
    conn.execute(sql.SQL('CREATE ROLE {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS').format(sql.Identifier(name),sql.SQL('LOGIN' if password else 'NOLOGIN')))
    if password:
        # Password never forms an output or command-line argument; SQL literal quoting is driver-owned.
        conn.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
    conn.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(name),sql.Literal(MARKER)))


def _credentials(settings,persist_env):
    updates={}
    for user_key,password_key,default in [('db_user','db_password','ds_project_reader'),('db_restricted_user','db_restricted_password','ds_project_seller_reader')]:
        user=getattr(settings,user_key)
        password=getattr(settings,password_key).get_secret_value()
        if not password:
            user=default; password=secrets.token_urlsafe(36)
            updates[user_key.upper()]=user; updates[password_key.upper()]=password
        if user==OWNER or user==settings.db_admin_user:
            raise StoreError('只读身份不得复用管理或表所有者角色。',409,'unsafe_database_role')
    if updates:
        if not persist_env:
            raise StoreError('隔离管理调用必须显式配置两个只读账号与密码。',400,'database_not_configured')
        _env_update(ROOT/'.env',updates)
        settings=Settings()
    if settings.db_user==settings.db_restricted_user:
        raise StoreError('全量与受限身份必须使用不同角色。',409,'unsafe_database_role')
    return settings


def bootstrap(settings, *, persist_env=True):
    """Create only a missing target DB; never reset an existing role or database."""
    settings=_credentials(settings,persist_env)
    with admin_connect(settings,'postgres') as conn:
        with conn.transaction():
            _role(conn,OWNER)
            _role(conn,settings.db_user,settings.db_password.get_secret_value())
            _role(conn,settings.db_restricted_user,settings.db_restricted_password.get_secret_value())
        exists=conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(settings.db_name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(settings.db_name),sql.Identifier(OWNER)))
    result=migrate(settings)
    if persist_env: _env_update(ROOT/'.env',{'DB_ENABLED':'true'})
    return {'database':settings.db_name,'created_database':not bool(exists),**result}


def migrate(settings):
    with admin_connect(settings) as conn:
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(490103817)')
            schema=conn.execute("SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname='olist'").fetchone()
            if schema and schema[0]!=MARKER:
                raise StoreError('olist 已存在且无本项目标记；需先人工盘点，拒绝覆盖。',409,'database_object_conflict')
            if not schema:
                if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname='ds_meta'").fetchone():
                    raise StoreError('ds_meta 已存在；拒绝覆盖非项目元数据。',409,'database_object_conflict')
                conn.execute((ROOT/'db/migrations/001_olist.sql').read_text(encoding='utf-8'))
            versions=[r[0] for r in conn.execute('SELECT version FROM ds_meta.schema_migrations ORDER BY version').fetchall()]
            if 2 not in versions:
                # A pre-existing untracked view is a conflict, never CREATE OR REPLACE it.
                collisions=conn.execute("SELECT relname FROM pg_class JOIN pg_namespace n ON n.oid=relnamespace WHERE n.nspname='olist' AND relname=ANY(%s)",(list(VIEWS),)).fetchall()
                if collisions: raise StoreError('分析视图名称冲突，拒绝覆盖。',409,'database_object_conflict')
                conn.execute((ROOT/'db/migrations/002_views.sql').read_text(encoding='utf-8'))
            _verify_schema(conn)
            _permissions(conn,settings)
            return {'schema':'olist','migration_versions':[1,2],'reader':settings.db_user,'restricted_reader':settings.db_restricted_user}


def _verify_schema(conn):
    """A migration number is insufficient evidence that existing objects still match."""
    schemas=conn.execute("SELECT n.nspname,r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname IN ('olist','ds_meta') ORDER BY n.nspname").fetchall()
    if schemas!=[('ds_meta',OWNER),('olist',OWNER)]:
        raise StoreError('项目 schema 的所有者不匹配，拒绝更改。',409,'database_schema_drift')
    expected_foreign={'customers':set(),'products':set(),'sellers':set(),'orders':{('customer_id','customers')},
        'order_items':{('order_id','orders'),('product_id','products'),('seller_id','sellers')},
        'order_payments':{('order_id','orders')},'order_reviews':{('order_id','orders')}}
    for name,columns in DEFINITIONS.items():
        info=conn.execute("SELECT c.relkind,r.rolname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='olist' AND c.relname=%s",(name,)).fetchone()
        actual=conn.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod) FROM pg_attribute a WHERE a.attrelid=to_regclass(%s) AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",('olist.'+name,)).fetchall()
        expected=[(column,'timestamp without time zone' if kind=='timestamp' else 'numeric(18,2)' if kind.startswith('numeric') else kind) for column,kind in columns]
        primary=conn.execute("SELECT a.attname FROM pg_constraint p CROSS JOIN LATERAL unnest(p.conkey) WITH ORDINALITY k(num,position) JOIN pg_attribute a ON a.attrelid=p.conrelid AND a.attnum=k.num WHERE p.conrelid=to_regclass(%s) AND p.contype='p' ORDER BY k.position",('olist.'+name,)).fetchall()
        foreign=conn.execute("SELECT a.attname,c.relname FROM pg_constraint p JOIN pg_class c ON c.oid=p.confrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=p.conrelid AND a.attnum=p.conkey[1] WHERE p.conrelid=to_regclass(%s) AND p.contype='f' AND n.nspname='olist' AND p.convalidated",('olist.'+name,)).fetchall()
        if info!=('r',OWNER) or actual!=expected or [row[0] for row in primary]!=KEYS[name] or set(foreign)!=expected_foreign[name]:
            raise StoreError(f'现有 olist.{name} 的所有者、列类型或键约束与版本不匹配，拒绝覆盖。',409,'database_schema_drift')
    for name in VIEWS:
        info=conn.execute("SELECT c.relkind,r.rolname,c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='olist' AND c.relname=%s",(name,)).fetchone()
        if not info or info[0:2]!=('v',OWNER) or 'security_invoker=true' not in (info[2] or []):
            raise StoreError(f'视图 {name} 与安全调用者配置不符。',409,'database_schema_drift')


def _permissions(conn,settings):
    for role in (settings.db_user,settings.db_restricted_user):
        _role(conn,role,'configured')
        inherited=conn.execute('SELECT r.rolname FROM pg_roles r WHERE r.rolname<>%s AND pg_has_role(%s,r.oid,\'USAGE\')',(role,role)).fetchall()
        if inherited:
            raise StoreError('项目只读角色继承了其他角色，请人工核验。',409,'unsafe_database_role')
        unsafe=conn.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='olist' AND (pg_has_role(%s,c.relowner,'USAGE') OR has_table_privilege(%s,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))",(role,role)).fetchall()
        if unsafe or conn.execute("SELECT has_schema_privilege(%s,'olist','CREATE')",(role,)).fetchone()[0]:
            raise StoreError('项目查询身份已具备业务对象所有者或写权限，拒绝继续授权。',409,'unsafe_database_role')
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(settings.db_name),sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA olist TO {}').format(sql.Identifier(role)))
        conn.execute(sql.SQL('ALTER ROLE {} IN DATABASE {} SET default_transaction_read_only=on').format(sql.Identifier(role),sql.Identifier(settings.db_name)))
    conn.execute(sql.SQL('GRANT SELECT ON {} TO {}').format(sql.SQL(',').join(sql.Identifier('olist',name) for name in TABLES+VIEWS),sql.Identifier(settings.db_user)))
    conn.execute(sql.SQL('GRANT SELECT ON olist.sellers TO {}').format(sql.Identifier(settings.db_restricted_user)))
    # Preserve policies: add project policies only on our own marked schema; never drop any policy.
    conn.execute('ALTER TABLE olist.sellers ENABLE ROW LEVEL SECURITY')
    conn.execute('ALTER TABLE olist.sellers FORCE ROW LEVEL SECURITY')
    policy_names={row[0] for row in conn.execute("SELECT polname FROM pg_policy WHERE polrelid='olist.sellers'::regclass").fetchall()}
    if policy_names-{'ds_full_reader','ds_city_reader'}:
        raise StoreError('sellers 含额外行级策略，需人工确认，拒绝潜在范围放宽。',409,'database_policy_drift')
    for name,role,predicate in [('ds_full_reader',settings.db_user,'true'),('ds_city_reader',settings.db_restricted_user,"seller_city = 'santo andre'")]:
        found=conn.execute("SELECT polpermissive,polcmd,polroles,pg_get_expr(polqual,polrelid),polwithcheck IS NULL FROM pg_policy WHERE polrelid='olist.sellers'::regclass AND polname=%s",(name,)).fetchone()
        role_oid=conn.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(role,)).fetchone()[0]
        expected_qual='true' if name=='ds_full_reader' else "(seller_city = 'santo andre'::text)"
        if found and (found[0:3]!=(True,'r',[role_oid]) or found[3]!=expected_qual or not found[4]):
            raise StoreError('已有 sellers 项目策略发生角色或表达式漂移，拒绝覆盖。',409,'database_policy_drift')
        if not found:
            conn.execute(sql.SQL('CREATE POLICY {} ON olist.sellers FOR SELECT TO {} USING ({})').format(sql.Identifier(name),sql.Identifier(role),sql.SQL(predicate)))


def _file_info(path,columns,keys):
    if not path.is_file():
        raise StoreError(f'找不到源 CSV：{path.name}。',404,'database_import_missing_file')
    with path.open('rb') as source: initial_hash=hashlib.file_digest(source,'sha256').hexdigest()
    header_expected=[name.replace('product_name_length','product_name_lenght').replace('product_description_length','product_description_lenght') for name,_ in columns]
    count=0; seen=set(); missing={name:0 for name,_ in columns}
    with path.open('r',encoding='utf-8-sig',newline='') as stream:
        reader=csv.reader(stream)
        if next(reader,None)!=header_expected:
            raise StoreError(f'{path.name} 表头不匹配。',422,'database_import_invalid')
        key_indexes=[header_expected.index(key) for key in keys]
        for row in reader:
            count+=1
            if len(row)!=len(columns): raise StoreError(f'{path.name} 第 {count} 条字段数异常。',422,'database_import_invalid')
            key=tuple(row[index] for index in key_indexes)
            if any(not value for value in key) or key in seen:
                raise StoreError(f'{path.name} 第 {count} 条主键为空或重复，导入已拒绝。',422,'database_import_invalid')
            seen.add(key)
            for index,(name,kind) in enumerate(columns):
                value=row[index]
                if value=='':
                    missing[name]+=1
                    if name in keys or name in ('customer_unique_id','order_status','price','freight_value','payment_value','review_score','customer_id','product_id','seller_id'):
                        raise StoreError(f'{path.name} 数据记录 {count}（含表头第 {count+1} 条 CSV 记录）字段 {name} 缺失。',422,'database_import_invalid')
                    continue
                try:
                    if kind=='integer' or kind.startswith('numeric'):
                        number=Decimal(value)
                        if not number.is_finite(): raise ValueError('nonfinite')
                        if kind=='integer' and (number!=number.to_integral_value() or not -(2**31)<=number<2**31): raise ValueError('integer')
                        if kind.startswith('numeric') and (number<0 or abs(number)>=10**16 or number!=number.quantize(Decimal('.01'))): raise ValueError('numeric')
                        if name=='review_score' and not 1<=number<=5: raise ValueError('score')
                    elif kind=='timestamp':
                        parsed=datetime.fromisoformat(value)
                        if parsed.tzinfo is not None: raise ValueError('timezone')
                except (ValueError,InvalidOperation,OverflowError):
                    raise StoreError(f'{path.name} 数据记录 {count}（含表头第 {count+1} 条 CSV 记录）字段 {name} 类型或取值无效，原值未输出。',422,'database_import_invalid') from None
    with path.open('rb') as source: digest=hashlib.file_digest(source,'sha256').hexdigest()
    if digest!=initial_hash: raise StoreError(f'{path.name} 在预校验期间发生变化。',409,'database_import_source_changed')
    return {'name':path.name,'sha256':digest,'row_count':count,'missing_cells':missing,'duplicate_keys':0,'empty_keys':0,
            'record_numbering':'CSV header is record 1; data record N is CSV record N+1, not physical line number (multiline text supported)'}


def import_dataset(settings):
    directory=settings.db_dataset_dir.resolve()
    files={name:_file_info(directory/f'olist_{name}_dataset.csv',columns,KEYS[name]) for name,columns in DEFINITIONS.items()}
    dataset_hash=hashlib.sha256(json.dumps({n:f['sha256'] for n,f in files.items()},sort_keys=True).encode()).hexdigest()
    batch_id=uuid.uuid4().hex
    with admin_connect(settings) as conn:
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(490103817)')
            _verify_schema(conn)
            old=conn.execute('SELECT batch_id,row_counts FROM ds_meta.import_batches WHERE dataset_hash=%s',(dataset_hash,)).fetchone()
            counts={name:conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier('olist',name))).fetchone()[0] for name in DEFINITIONS}
            if old:
                if counts!=old[1]: raise StoreError('现库行数与已登记批次不符，拒绝静默跳过。',409,'database_import_conflict')
                return {'batch_id':old[0],'dataset_hash':dataset_hash,'row_counts':counts,'idempotent':True,'files':files}
            if any(counts.values()):
                raise StoreError('业务表已有数据且没有相同批次；拒绝覆盖、去重或追加。',409,'database_import_conflict')
            for name,columns in DEFINITIONS.items():
                temp=f'ds_stage_{name}'
                conn.execute(sql.SQL('CREATE TEMP TABLE {} ({},_csv_record_number bigint GENERATED ALWAYS AS IDENTITY) ON COMMIT DROP').format(sql.Identifier(temp),sql.SQL(',').join(sql.SQL('{} text').format(sql.Identifier(col)) for col,_ in columns)))
                # CSV mode preserves quoted multiline text and NULL versus quoted empty string.
                with conn.cursor() as cursor:
                    with cursor.copy(sql.SQL('COPY {} ({}) FROM STDIN WITH (FORMAT CSV, HEADER true, ENCODING \'UTF8\')').format(sql.Identifier(temp),sql.SQL(',').join(sql.Identifier(col) for col,_ in columns))) as copy:
                        with (directory/files[name]['name']).open('rb') as source:
                            while block:=source.read(1024*1024): copy.write(block)
                for column,parent,parent_key in FOREIGN_KEYS.get(name,[]):
                    invalid=conn.execute(sql.SQL('SELECT t._csv_record_number FROM {} t LEFT JOIN {} p ON t.{}=p.{} WHERE p.{} IS NULL ORDER BY t._csv_record_number LIMIT 1').format(sql.Identifier(temp),sql.Identifier('olist',parent),sql.Identifier(column),sql.Identifier(parent_key),sql.Identifier(parent_key))).fetchone()
                    if invalid:
                        record_number=invalid[0]
                        raise StoreError(f"{files[name]['name']} 数据记录 {record_number}（含表头第 {record_number+1} 条 CSV 记录）字段 {column} 外键无匹配，整批回滚，原值未输出。",422,'database_import_invalid')
                expressions=[]
                for column,type_name in columns:
                    base=sql.SQL("NULLIF({},'')").format(sql.Identifier(column))
                    if type_name=='integer':
                        # Olist has nullable integral columns serialized as 40.0. Reject fractions rather than round.
                        fractional=conn.execute(sql.SQL('SELECT count(*) FROM {} WHERE {} IS NOT NULL AND ({}::numeric <> trunc({}::numeric) OR {}::numeric NOT BETWEEN -2147483648 AND 2147483647)').format(sql.Identifier(temp),base,base,base,base)).fetchone()[0]
                        if fractional: raise StoreError(f'{name}.{column} 存在非整数值。',422,'database_import_invalid')
                        expression=sql.SQL('{}::numeric::integer').format(base)
                    elif type_name.startswith('numeric'):
                        invalid=conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE {}::numeric IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric) OR {}::numeric <> round({}::numeric,2)").format(sql.Identifier(temp),base,base,base)).fetchone()[0]
                        if invalid: raise StoreError(f'{name}.{column} 小数精度超出2位，拒绝舍入。',422,'database_import_invalid')
                        expression=sql.SQL('{}::{}').format(base,sql.SQL(type_name))
                    else: expression=sql.SQL('{}::{}').format(base,sql.SQL(type_name))
                    expressions.append(expression)
                conn.execute(sql.SQL('INSERT INTO {} SELECT {} FROM {}').format(sql.Identifier('olist',name),sql.SQL(',').join(expressions),sql.Identifier(temp)))
                actual=conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier('olist',name))).fetchone()[0]
                if actual!=files[name]['row_count']: raise StoreError('导入数量校验失败，整批回滚。',422,'database_import_invalid')
                # Re-read the hash after COPY to catch source changes between validation and import.
                with (directory/files[name]['name']).open('rb') as source:
                    if hashlib.file_digest(source,'sha256').hexdigest()!=files[name]['sha256']:
                        raise StoreError('CSV 在导入期间发生变化，整批回滚。',409,'database_import_source_changed')
                counts[name]=actual
            for name in DEFINITIONS:
                conn.execute(sql.SQL('ANALYZE {}').format(sql.Identifier('olist',name)))
            quality={'primary_keys':'all validated','foreign_keys':'all enforced','missing_values':'preserved as NULL; no imputation or deduplication','optional_files':'geolocation/translation not imported'}
            conn.execute('INSERT INTO ds_meta.import_batches(batch_id,dataset_hash,files,quality,row_counts) VALUES(%s,%s,%s,%s,%s)',(batch_id,dataset_hash,Jsonb(files),Jsonb(quality),Jsonb(counts)))
            return {'batch_id':batch_id,'dataset_hash':dataset_hash,'row_counts':counts,'idempotent':False,'files':files,'quality':quality}


def status(settings):
    with admin_connect(settings,'postgres') as conn:
        exists=bool(conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(settings.db_name,)).fetchone())
        version=conn.execute('SHOW server_version').fetchone()[0]
    result={'database':settings.db_name,'exists':exists,'server_version':version,'host':settings.db_host,'port':settings.db_port}
    if exists:
        with admin_connect(settings) as conn:
            tables=[r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='olist' ORDER BY tablename").fetchall()]
            result['row_counts']={name:conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier('olist',name))).fetchone()[0] for name in tables}
            if conn.execute("SELECT to_regclass('ds_meta.import_batches')").fetchone()[0]:
                result['batches']=[dict(zip(['batch_id','dataset_hash','completed_at'],row)) for row in conn.execute('SELECT batch_id,dataset_hash,completed_at FROM ds_meta.import_batches ORDER BY completed_at').fetchall()]
    return result
