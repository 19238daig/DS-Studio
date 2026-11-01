import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { App, Button, Collapse, Drawer, Input, Progress, Select, Table, Tag, Tree } from 'antd';
import type { TreeDataNode } from 'antd';
import {
  CloudUploadOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { api, uploadFiles } from '../../shared/api/client';
import type { Catalog, Dataset, Health, Profile, DatabasePreview } from '../../shared/api/types';
import { useUI, emptyDraft } from '../../shared/state';
import { Blank, bytes, ErrorNotice, errorMessage } from '../../shared/ui';

interface FileNode extends TreeDataNode {
  fileId?: string;
  children?: FileNode[];
}
export function fileTree(files: Dataset[]): FileNode[] {
  const roots: FileNode[] = [];
  for (const file of files) {
    const original = (file.relative_path || file.name).replace(/\\/g, '/').replace(/^KB\//, '');
    const parts =
      file.source === 'kb' ? ['知识库', ...original.split('/').slice(0, -1)] : ['会话上传'];
    let branch = roots;
    let path = '';
    for (const folder of parts) {
      path += '/' + folder;
      let node = branch.find((n) => n.key === path);
      if (!node) {
        node = { key: path, title: folder, children: [] };
        branch.push(node);
      }
      branch = node.children!;
    }
    branch.push({
      key: file.file_id,
      title: file.name,
      icon: <FileTextOutlined />,
      fileId: file.file_id,
      isLeaf: true,
    });
  }
  return roots;
}
export function useFiles(sid: string) {
  const kb = useQuery({ queryKey: ['kb'], queryFn: ({ signal }) => api.kb(signal) });
  const uploaded = useQuery({
    queryKey: ['files', sid],
    queryFn: ({ signal }) => api.files(sid, signal),
    enabled: !!sid,
    refetchInterval: (q) =>
      q.state.data?.items.some((f) => ['queued', 'running'].includes(f.profile_status ?? ''))
        ? 3000
        : false,
  });
  return {
    files: [
      ...(kb.data?.items ?? []),
      ...(uploaded.data?.items ?? []).filter((f) => f.source !== 'kb'),
    ],
    error: kb.error || uploaded.error,
    reload: () => {
      void kb.refetch();
      void uploaded.refetch();
    },
  };
}
function UploadBox({ sid, health }: { sid: string; health?: Health }) {
  const { message } = App.useApp();
  const query = useQueryClient();
  const patch = useUI((s) => s.patch);
  const [progress, setProgress] = useState<number | null>(null),
    [names, setNames] = useState<string[]>([]);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), [sid]);
  async function upload(files: File[]) {
    if (!files.length) return;
    const limit = Math.min(
      health?.limits.max_upload_bytes ?? 1024 ** 3,
      health?.limits.max_task_input_bytes ?? 1024 ** 3,
    );
    const total = files.reduce((sum, f) => sum + f.size, 0);
    if (files.some((f) => !/\.(csv|xlsx?|html?)$/i.test(f.name))) {
      void message.error('仅支持 CSV、Excel 和静态 HTML。');
      return;
    }
    if (
      files.length > (health?.limits.max_files_per_task ?? 10) ||
      total > limit ||
      files.some((f) => f.size > limit)
    ) {
      void message.error(`每批至多 10 个文件，总容量不超过 ${bytes(limit)}。`);
      return;
    }
    const abort = new AbortController();
    controller.current = abort;
    setProgress(0);
    setNames(files.map((f) => f.name));
    try {
      const result = await uploadFiles(sid, files, setProgress, abort.signal);
      const before = useUI.getState().drafts[sid] ?? emptyDraft;
      const next = [...new Set([...before.fileIds, ...result.items.map((f) => f.file_id)])];
      if (next.length <= 10) patch(sid, { fileIds: next });
      void query.invalidateQueries({ queryKey: ['files', sid] });
      void query.invalidateQueries({ queryKey: ['session', sid] });
      void message.success('传输完成，文件画像将在后台处理。');
    } catch (e) {
      if ((e as Error).name !== 'AbortError') void message.error(errorMessage(e));
      else void message.info('上传已取消，已运行的分析任务不受影响。');
    } finally {
      setProgress(null);
      controller.current = null;
    }
  }
  return (
    <div className="ds-upload">
      <label
        className={'ds-upload-target ' + (progress !== null ? 'is-disabled' : '')}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          if (progress === null) void upload([...e.dataTransfer.files]);
        }}
      >
        <CloudUploadOutlined />
        <strong>上传分析数据</strong>
        <span>拖入或选择文件 · CSV / Excel / HTML</span>
        <small>每批最多 {bytes(health?.limits.max_upload_bytes ?? 1024 ** 3)}</small>
        <input
          aria-label="选择上传文件"
          type="file"
          multiple
          accept=".csv,.xlsx,.xls,.html,.htm"
          disabled={progress !== null}
          onChange={(e) => {
            void upload([...(e.target.files ?? [])]);
            e.target.value = '';
          }}
        />
      </label>
      {progress !== null && (
        <div role="status">
          <span>{names.join('、')}</span>
          <Progress percent={progress} size="small" />
          <small>{progress === 100 ? '服务端正在登记文件…' : '正在传输文件'}</small>
          <Button size="small" icon={<StopOutlined />} onClick={() => controller.current?.abort()}>
            取消上传
          </Button>
        </div>
      )}
    </div>
  );
}
function DatabasePicker({ sid }: { sid: string }) {
  const draft = useUI((s) => s.drafts[sid] ?? emptyDraft);
  const patch = useUI((s) => s.patch);
  const sources = useQuery({ queryKey: ['sources'], queryFn: ({ signal }) => api.sources(signal) });
  return (
    <div className="ds-database">
      <div className="ds-section-title">
        <DatabaseOutlined />
        PostgreSQL 数据库
      </div>
      {sources.error ? (
        <ErrorNotice error={sources.error} />
      ) : (
        <Select
          mode="multiple"
          aria-label="数据库来源"
          placeholder="选择只读来源"
          className="ds-full"
          value={draft.sourceIds ?? []}
          options={(sources.data?.items ?? []).map((s) => ({
            value: s.source_id,
            label: s.name,
            disabled: !s.configured,
          }))}
          onChange={(ids) =>
            patch(sid, {
              sourceIds: ids,
              tables: Object.fromEntries(
                Object.entries(draft.tables ?? {}).filter(([key]) => ids.includes(key)),
              ),
            })
          }
        />
      )}
      {(draft.sourceIds ?? []).map((source) => (
        <DatabaseTables key={source} sid={sid} source={source} />
      ))}
      {!(sources.data?.items ?? []).some((s) => s.configured) && (
        <p className="ds-hint">数据库尚未启用，可先使用上传文件或知识库。</p>
      )}
    </div>
  );
}
function DatabaseTables({ sid, source }: { sid: string; source: string }) {
  const draft = useUI((s) => s.drafts[sid] ?? emptyDraft);
  const patch = useUI((s) => s.patch);
  const { message } = App.useApp();
  const catalog = useQuery({
    queryKey: ['catalog', source],
    queryFn: ({ signal }) => api.catalog(source, signal),
  });
  const [preview, setPreview] = useState<DatabasePreview | null>(null);
  const [busy, setBusy] = useState(false),
    [table, setTable] = useState('');
  async function load() {
    setBusy(true);
    try {
      setPreview(await api.databasePreview(sid, source, table));
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  if (catalog.error) return <ErrorNotice error={catalog.error} />;
  const data = catalog.data;
  const tables = data?.tables ?? [];
  return (
    <div className="ds-schema">
      <small>授权表 · {source}</small>
      <Select
        className="ds-full"
        mode="multiple"
        aria-label={`${source} 授权表`}
        placeholder="清空表示不读取任何表"
        value={draft.tables?.[source] ?? tables.map((t) => t.qualified_name)}
        options={tables.map((t) => ({ value: t.qualified_name, label: t.qualified_name }))}
        onChange={(ids) => patch(sid, { tables: { ...draft.tables, [source]: ids } })}
      />
      <p className="ds-hint">{data?.scope_description || '只读查询；清空表选择不会自动恢复。'}</p>
      <Collapse
        size="small"
        ghost
        items={[
          {
            key: 'fields',
            label: '字段与数据预览',
            children: (
              <>
                <Select
                  className="ds-full"
                  aria-label={`${source} 预览表`}
                  value={table || undefined}
                  placeholder="选择表"
                  onChange={setTable}
                  options={tables.map((t) => ({ value: t.qualified_name, label: t.name }))}
                />
                {table && (
                  <>
                    <p className="ds-hint">
                      {tables
                        .find((t) => t.qualified_name === table)
                        ?.columns.map((c) => c.name)
                        .join(' · ')}
                    </p>
                    <Button loading={busy} onClick={load}>
                      读取前 20 行
                    </Button>
                  </>
                )}
              </>
            ),
          },
        ]}
      />
      <Drawer
        title={`数据预览 · ${table}`}
        open={!!preview}
        onClose={() => setPreview(null)}
        size="large"
      >
        {preview && (
          <>
            <p className="ds-hint">有界预览不代表完整统计结果；查询已按当前数据库身份执行。</p>
            <Table
              size="small"
              rowKey={(_, i) => String(i)}
              dataSource={preview.preview}
              scroll={{ x: 'max-content' }}
              pagination={false}
              columns={preview.columns.map((c) => ({
                title: c.name,
                dataIndex: c.name,
                render: (v: unknown) => String(v ?? '—'),
              }))}
            />
          </>
        )}
      </Drawer>
    </div>
  );
}
export function SourcePicker({
  sid,
  health,
  expanded = false,
}: {
  sid: string;
  health?: Health;
  expanded?: boolean;
}) {
  const { files, error, reload } = useFiles(sid);
  const [filter, setFilter] = useState(''),
    [viewId, setViewId] = useState<string | null>(null);
  const query = useQueryClient();
  const { message } = App.useApp();
  const draft = useUI((s) => s.drafts[sid] ?? emptyDraft);
  const patch = useUI((s) => s.patch);
  const view = files.find((file) => file.file_id === viewId) ?? null;
  const visibleFiles = useMemo(
    () =>
      files.filter((f) =>
        (f.name + ' ' + (f.relative_path ?? '')).toLowerCase().includes(filter.toLowerCase()),
      ),
    [files, filter],
  );
  const tree = useMemo(() => fileTree(visibleFiles), [visibleFiles]);
  async function refresh() {
    try {
      const t = await api.refreshKB();
      void message.success('知识库刷新任务已受理。');
      const poll = async () => {
        const record = await api.task(t.task_id);
        if (['queued', 'running'].includes(record.status)) return false;
        await query.invalidateQueries({ queryKey: ['kb'] });
        return true;
      };
      for (let i = 0; i < 30; i++) {
        if (await poll()) return;
        await new Promise((r) => setTimeout(r, 1000));
      }
      void message.info('刷新仍在后台处理，稍后可点击重新读取。');
    } catch (e) {
      void message.error(errorMessage(e));
    }
  }
  return (
    <div className={'ds-sources ' + (expanded ? 'ds-sources-expanded' : '')}>
      <div className="ds-section-title">
        分析数据 <Tag>{draft.fileIds.length} 份已选</Tag>
        <Button
          type="text"
          size="small"
          aria-label="重新读取文件"
          icon={<ReloadOutlined />}
          onClick={reload}
        />
      </div>
      <UploadBox sid={sid} health={health} />
      <Input
        aria-label="搜索数据文件"
        placeholder="搜索文件或目录"
        prefix={<SearchOutlined />}
        allowClear
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      {error && <ErrorNotice error={error} retry={reload} />}
      <div className="ds-file-tree">
        {tree.length ? (
          <Tree
            checkable
            blockNode
            showIcon
            treeData={tree}
            checkedKeys={draft.fileIds}
            defaultExpandAll
            key={filter || 'tree'}
            onCheck={(_, info) => {
              const ids = info.checkedNodes
                .filter((n) => 'fileId' in n)
                .map((n) => String((n as FileNode).fileId));
              const hidden = draft.fileIds.filter(
                (id) => !visibleFiles.some((f) => f.file_id === id),
              );
              const all = [...new Set([...hidden, ...ids])];
              if (all.length > 10) {
                void message.warning('一次最多选择 10 份文件，请逐项选择。');
                return;
              }
              patch(sid, { fileIds: all });
            }}
            onSelect={(_, info) => {
              const fid = (info.node as FileNode).fileId;
              if (fid) setViewId(fid);
            }}
          />
        ) : (
          <Blank title={filter ? '没有匹配文件' : '还没有数据文件'} />
        )}
      </div>
      <Button size="small" type="text" onClick={refresh}>
        刷新本机知识库索引
      </Button>
      <DatabasePicker sid={sid} />
      <Drawer
        title={view?.name ?? '文件画像'}
        open={!!view}
        onClose={() => setViewId(null)}
        size="large"
      >
        {view && (
          <>
            <Tag>{view.source === 'kb' ? '知识库' : '会话上传'}</Tag>
            <Tag>{bytes(view.size)}</Tag>
            <p>
              画像状态：
              {view.profile_status === 'ready' ? '已就绪' : view.profile_status || '将按需解析'}
            </p>
            {view.profile_error && <ErrorNotice error={view.profile_error} />}
            {(view.profiles?.length ? view.profiles : view.profile ? [view.profile] : []).map(
              (p: Profile, i) => (
                <div key={i}>
                  <h3>
                    {p.sheet_or_table || '数据表'} · {p.row_count?.toLocaleString()} 行
                  </h3>
                  <Table
                    size="small"
                    pagination={false}
                    rowKey="name"
                    dataSource={p.columns.map((name) => ({ name, type: p.dtypes?.[name] ?? '—' }))}
                    columns={[
                      { title: '字段', dataIndex: 'name' },
                      { title: '类型', dataIndex: 'type' },
                    ]}
                  />
                  {p.quality_warnings?.map((w, j) => (
                    <p className="ds-hint" key={j}>
                      {w}
                    </p>
                  ))}
                </div>
              ),
            )}
          </>
        )}
      </Drawer>
    </div>
  );
}
