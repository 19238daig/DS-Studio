"""SQL execution with authoritative, session-bound immutable result snapshots."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import threading
import time
from datetime import date, datetime, time as daytime
from decimal import Decimal
from pathlib import Path

from psycopg import RawServerCursor

from src.database.catalog import catalog, describe, source_list
from src.database.connection import connect
from src.database.policy import validate_sql
from src.schemas import new_id, utcnow
from src.storage import StoreError, identifier
from src.performance import stage, traced_method
from src.validation import catalog_once, validation_count, validation_scope

REF_KEYS=('query_id','source_id','scope_fingerprint','metadata_artifact_id','result_artifact_id','sql_artifact_id')


def serial(value):
    if isinstance(value,Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value,float) and not math.isfinite(value):
        return None
    if isinstance(value,(datetime,date,daytime)):
        return value.isoformat()
    if isinstance(value,(bytes,memoryview)):
        return bytes(value).hex()
    if isinstance(value,dict):
        return {str(k):serial(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):
        return [serial(v) for v in value]
    return value


def cell(value):
    value=serial(value)
    return json.dumps(value,ensure_ascii=False,allow_nan=False) if isinstance(value,(dict,list)) else value


class DatabaseService:
    def __init__(self,store):
        self.store,self.settings=store,store.settings

    def list_sources(self):
        return source_list(self.settings)

    def catalog(self,source_id):
        from src.database.connection import SOURCES
        # Connection/role/profile identity is part of the key; the memo exists
        # only for the current validation boundary, never across model turns.
        key=(id(self.settings),source_id,self.settings.db_host,self.settings.db_port,
             self.settings.db_name,self.settings.db_user,self.settings.db_restricted_user,
             self.settings.db_enabled,json.dumps(SOURCES.get(source_id),sort_keys=True))
        def load():
            with stage(self.store,'database_catalog',source_id=source_id):
                return catalog(self.settings,source_id)
        return catalog_once(self.store,key,load)

    def _selected(self,task,source_id):
        self.store.get_session(task.session_id)
        record=self.store.get_task(task.task_id)
        if record['session_id']!=task.session_id:
            raise StoreError('任务数据库来源与会话不匹配。',403,'database_source_not_selected')
        if source_id not in getattr(task,'database_source_ids',[]):
            raise StoreError('数据库数据源未获本任务选择授权。',403,'database_source_not_selected')
        registered=record.get('spec',{})
        if (getattr(task,'database_source_ids',[])!=registered.get('database_source_ids',[]) or
                getattr(task,'database_tables',{})!=registered.get('database_tables',{})):
            raise StoreError('数据库来源选择与任务登记不一致。',403,'database_source_not_selected')

    def _tables(self,task,source_id,description):
        visible={t['qualified_name'] for t in description['tables']}
        selections=getattr(task,'database_tables',{})
        # Omission authorizes the source's visible tables; an explicitly empty
        # selection authorizes none. Clearing a table picker must never widen it.
        if source_id in selections:
            chosen={name if '.' in name else 'olist.'+name for name in selections[source_id]}
            if not chosen.issubset(visible):
                raise StoreError('选择的数据库表不存在或当前身份不可访问。',403,'database_table_not_allowed')
            visible &= chosen
        return visible

    def preview(self,task,source_id,table,limit=20,cancel_check=None):
        # Reuse the exact query policy/snapshot path; no alternate SQL shortcut.
        if not isinstance(limit,int) or not 1<=limit<=200:
            raise StoreError('预览行数必须在1至200。',422,'invalid_preview_limit')
        if table.startswith('olist.'):
            table=table[6:]
        identifier(table)
        return self.query(task,source_id,f'SELECT * FROM olist."{table}" LIMIT {limit}',cancel_check=cancel_check,
                          purpose='preview')

    def query(self,task,source_id,sql,params=None,cancel_check=None,*,purpose='analysis'):
        return self.query_batch(task,source_id,[{'sql':sql,'params':params or [],'purpose':purpose}],cancel_check)[0]

    @traced_method('database_query_batch')
    def query_batch(self,task,source_id,queries,cancel_check=None):
        self._selected(task,source_id)
        if not isinstance(queries,list) or not 1<=len(queries)<=20:
            raise StoreError('每批必须包含1至20条只读查询。',422,'invalid_sql_batch')
        cancelled=cancel_check or (lambda:bool(self.store.get_task(task.task_id).get('cancel_requested')))
        def check():
            if cancelled():
                raise InterruptedError('Database task cancellation requested')
        check()
        paths=[];pending=[]
        try:
            with connect(self.settings,source_id) as connection:
                with connection.transaction():
                    connection.execute("SELECT pg_catalog.set_config('statement_timeout',%s,true)",(str(self.settings.db_statement_timeout_seconds*1000),))
                    connection.execute("SELECT pg_catalog.set_config('lock_timeout','5000',true)")
                    connection.execute("SELECT pg_catalog.set_config('idle_in_transaction_session_timeout','30000',true)")
                    connection.execute("SELECT pg_catalog.set_config('search_path','pg_catalog, olist',true)")
                    description=describe(connection,source_id)
                    allowed=self._tables(task,source_id,description)
                    snapshot=connection.execute('SELECT pg_catalog.pg_current_snapshot()::text').fetchone()[0]
                    for query in queries:
                        check()
                        if not isinstance(query,dict):
                            raise StoreError('每条查询必须包含sql与params字段。',422,'invalid_sql_batch')
                        params=query.get('params')
                        if params is None:params=[]
                        if not isinstance(params,(list,tuple)):
                            raise StoreError('SQL参数必须为数组，SQL中使用$1、$2。',422,'invalid_sql_parameters')
                        sql=query.get('sql','')
                        policy=validate_sql(sql,allowed,params_count=len(params))
                        qid=new_id()
                        directory=self.store.safe_path(f'script_output/{task.session_id}/{task.task_id}/query_results/{qid}')
                        directory.mkdir(parents=True,exist_ok=False)
                        result_path=directory/'result.csv'
                        paths.append(directory)
                        started=time.perf_counter()
                        done=threading.Event()
                        def watch():
                            while not done.wait(.25):
                                try:
                                    if cancelled():
                                        connection.cancel_safe(timeout=2)
                                        return
                                except Exception:
                                    return
                        watcher=threading.Thread(target=watch,daemon=True)
                        watcher.start()
                        preview=[];row_count=0
                        try:
                            # Native $1 parameters remain intact for AST validation.
                            # https://www.psycopg.org/psycopg3/docs/advanced/cursors.html#raw-query-cursors
                            with RawServerCursor(connection,name='ds_'+qid) as cursor:
                                cursor.execute(sql,params)
                                type_oids=list({c.type_code for c in cursor.description})
                                type_names=dict(connection.execute('SELECT oid,pg_catalog.format_type(oid,NULL) FROM pg_catalog.pg_type WHERE oid=ANY(%s)',(type_oids,)).fetchall())
                                columns=[dict(name=c.name,type_oid=c.type_code,data_type=type_names.get(c.type_code,'unknown')) for c in cursor.description]
                                names=[c['name'] for c in columns]
                                if len(set(names))!=len(names):
                                    raise StoreError('结果列名重复，请为列设置不同别名。',422,'duplicate_sql_columns')
                                with result_path.open('w',encoding='utf-8-sig',newline='') as stream:
                                    writer=csv.writer(stream)
                                    writer.writerow(names)
                                    while rows:=cursor.fetchmany(self.settings.db_fetch_rows):
                                        check()
                                        row_count+=len(rows)
                                        if row_count>self.settings.db_max_result_rows:
                                            raise StoreError('查询结果超出完整结果容量；请改用数据库内聚合或专用导出，未返回截断统计。',413,'database_result_too_large')
                                        for row in rows:
                                            values=[serial(v) for v in row]
                                            if len(preview)<20:
                                                preview.append(dict(zip(names,values)))
                                            writer.writerow([cell(v) for v in row])
                                        stream.flush()
                                        if result_path.stat().st_size>self.settings.db_max_result_bytes:
                                            raise StoreError('完整查询结果超过配置字节限制，未返回截断结果。',413,'database_result_too_large')
                                check()
                                if result_path.stat().st_size>self.settings.db_max_result_bytes:
                                    raise StoreError('完整查询结果超过配置字节限制，未返回截断结果。',413,'database_result_too_large')
                        finally:
                            done.set();watcher.join(timeout=3)
                        pending.append(dict(query_id=qid,source_id=source_id,scope_fingerprint=description['scope_fingerprint'],
                            scope_description=description['scope_description'],database=description['database'],
                            database_user=description['user'],session_id=task.session_id,task_id=task.task_id,
                            snapshot_id=snapshot,queried_at=utcnow(),duration_seconds=round(time.perf_counter()-started,3),
                            sql=sql,params=serial(params),tables=policy['tables'],columns=columns,row_count=row_count,
                            preview=preview,scalar_metrics=preview[0] if row_count==1 else {},
                            purpose=query.get('purpose','analysis'),complete=True,directory=directory))
            check()
            # Publish only after the entire readonly batch has succeeded, then
            # refresh current authorization outside the long query snapshot.
            # Revocation during execution cannot be hidden by a tool scope.
            published=[self._publish(task,record) for record in pending]
            with validation_scope(self.store,task,fresh=True):
                for result in published:
                    self.authorize_snapshot(task,result['db_ref'])
            return published
        except BaseException as exc:
            # Registration spans several file indexes. Remove only records from
            # this failed batch before deleting its files; other tasks remain.
            with self.store.lock:
                qids={directory.name for directory in paths}
                artifacts=self.store._index('artifacts')
                artifacts={key:value for key,value in artifacts.items() if not (
                    value.get('task_id')==task.task_id and any('/query_results/'+qid+'/' in value.get('relative_path','') for qid in qids))}
                self.store._save_index('artifacts',artifacts)
                queries_index=self.store._index('database_queries')
                for qid in qids:queries_index.pop(qid,None)
                self.store._save_index('database_queries',queries_index)
            # Only this call's resolved, task-owned directories are removed.
            for directory in paths:
                expected=self.store.safe_path(f'script_output/{task.session_id}/{task.task_id}/query_results')
                if directory.resolve().is_relative_to(expected) and directory.exists():
                    for path in directory.iterdir():
                        if path.is_file():path.unlink()
                    directory.rmdir()
            if cancelled() and isinstance(exc,StoreError) and exc.code=='database_error':
                raise InterruptedError('Database task cancellation requested') from None
            raise

    def _publish(self,task,record):
        directory=record.pop('directory')
        # Query input is not a credential source. Still redact exact configured
        # credential values if a caller copied one into a literal or parameter.
        def redact(value):
            if isinstance(value,str):
                for field in ('db_password','db_admin_password','db_restricted_password','api_key'):
                    secret=getattr(self.settings,field).get_secret_value()
                    if secret:value=value.replace(secret,'[REDACTED]')
                return value
            if isinstance(value,list):return [redact(x) for x in value]
            if isinstance(value,dict):return {k:redact(v) for k,v in value.items()}
            return value
        record=redact(record)
        source_id=record['source_id']
        sql_path=directory/'query.json'
        sql_path.write_text(json.dumps({'sql':record['sql'],'params':record['params']},ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
        result=self.store.register_artifact(directory/'result.csv',task.task_id,task.session_id,'table',[source_id])
        sql_artifact=self.store.register_artifact(sql_path,task.task_id,task.session_id,'sql',[source_id])
        record.update(result_artifact_id=result.artifact_id,result_hash=result.hash,sql_artifact_id=sql_artifact.artifact_id,
                      sql_hash=sql_artifact.hash)
        metadata_path=directory/'metadata.json'
        metadata_path.write_text(json.dumps(record,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
        metadata=self.store.register_artifact(metadata_path,task.task_id,task.session_id,'database_query',[source_id])
        ref={key:record[key] for key in ('query_id','source_id','scope_fingerprint','result_artifact_id','sql_artifact_id')}
        ref['metadata_artifact_id']=metadata.artifact_id
        with self.store.lock:
            index=self.store._index('database_queries')
            index[record['query_id']]={**ref,'session_id':task.session_id,'task_id':task.task_id}
            self.store._save_index('database_queries',index)
        return {**record,'metadata_artifact_id':metadata.artifact_id,'db_ref':ref}

    def authorize_snapshot(self,task,ref):
        validation_count(self.store,'snapshot_checks')
        if not isinstance(ref,dict):
            raise StoreError('数据库快照引用格式无效。',422,'invalid_database_snapshot')
        qid=identifier(ref.get('query_id',''))
        record=self.store._index('database_queries').get(qid)
        if not record or record['session_id']!=task.session_id:
            raise StoreError('数据库快照不属于本会话。',404,'database_snapshot_not_found')
        for key in ('source_id','scope_fingerprint','metadata_artifact_id','result_artifact_id','sql_artifact_id'):
            if ref.get(key)!=record[key]:
                raise StoreError('数据库快照引用与登记记录不一致。',409,'invalid_database_snapshot')
        self._selected(task,record['source_id'])
        metadata_artifact=self.store.get_artifact(record['metadata_artifact_id'])
        if metadata_artifact.kind!='database_query' or metadata_artifact.task_id!=record['task_id'] or metadata_artifact.session_id!=task.session_id or metadata_artifact.source_ids!=[record['source_id']]:
            raise StoreError('数据库快照来源身份不一致。',409,'invalid_database_snapshot')
        metadata=json.loads(self.store.resolve_artifact(metadata_artifact.artifact_id).read_text(encoding='utf-8'))
        for key,kind,hash_key in [('result_artifact_id','table','result_hash'),('sql_artifact_id','sql','sql_hash')]:
            artifact=self.store.get_artifact(record[key])
            if artifact.task_id!=record['task_id'] or artifact.session_id!=task.session_id or artifact.kind!=kind or artifact.hash!=metadata[hash_key] or artifact.source_ids!=[record['source_id']]:
                raise StoreError('数据库结果或SQL来源校验失败。',409,'invalid_database_snapshot')
            self.store.resolve_artifact(artifact.artifact_id)
        if any(metadata.get(key)!=record[key] for key in ('query_id','session_id','task_id','source_id','scope_fingerprint','result_artifact_id','sql_artifact_id')) or not metadata.get('complete'):
            raise StoreError('数据库快照内容不完整或身份不一致。',409,'invalid_database_snapshot')
        description=self.catalog(record['source_id'])
        if record['scope_fingerprint']!=description['scope_fingerprint']:
            raise StoreError('数据库连接身份或权限范围已变化，请重新查询。',409,'database_scope_changed')
        if not set(metadata['tables']).issubset(self._tables(task,record['source_id'],description)):
            raise StoreError('数据库快照超出本任务选择的表。',403,'database_table_not_allowed')
        return {**metadata,'metadata_artifact_id':metadata_artifact.artifact_id,'db_ref':{k:record[k] for k in REF_KEYS}}
