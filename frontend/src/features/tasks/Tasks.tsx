import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { App, Alert, Button, Collapse, Select, Table, Tag } from 'antd';
import { DownloadOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons';
import { api } from '../../shared/api/client';
import type { Dataset, EventRecord, Session, Task } from '../../shared/api/types';
import { terminal } from '../../shared/api/types';
import { useUI, emptyDraft } from '../../shared/state';
import {
  Blank,
  date,
  duration,
  ErrorNotice,
  errorMessage,
  roleName,
  Status,
  tokens,
} from '../../shared/ui';
export function EventLog({ taskId, active }: { taskId: string; active: boolean }) {
  const [records, setRecords] = useState<EventRecord[]>([]),
    [cursor, setCursor] = useState(0);
  const { message } = App.useApp();
  useEffect(() => {
    setRecords([]);
    setCursor(0);
  }, [taskId]);
  const events = useQuery({
    queryKey: ['events', taskId, cursor],
    queryFn: ({ signal }) => api.events(taskId, cursor, signal),
    enabled: !!taskId,
    refetchInterval: active ? 2500 : false,
  });
  useEffect(() => {
    if (!events.data?.items.length) return;
    setRecords((old) => {
      const map = new Map(old.map((e) => [e.seq, e]));
      events.data.items.forEach((e) => map.set(e.seq, e));
      return [...map.values()].sort((a, b) => a.seq - b.seq).slice(-2000);
    });
    setCursor(events.data.cursor);
  }, [events.data]);
  async function download() {
    try {
      const all = await api.events(taskId);
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(all.items, null, 2)], { type: 'application/json' }),
      );
      const a = document.createElement('a');
      a.href = url;
      a.download = `task-${taskId.slice(0, 8)}-events.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      void message.error(errorMessage(e));
    }
  }
  return (
    <div>
      {events.error && <ErrorNotice error={events.error} />}
      <div className="ds-log-toolbar">
        <small>最近 {records.length} 条，完整审计仍在后端保存</small>
        <Button size="small" type="text" icon={<DownloadOutlined />} onClick={download}>
          导出记录
        </Button>
      </div>
      {records.length ? (
        <ol className="ds-event-list">
          {records.map((e) => (
            <li
              key={e.seq}
              className={e.type.includes('error') || e.type === 'failed' ? 'is-error' : ''}
            >
              <div>
                <time>{date(e.created_at)}</time>
                <span>
                  {e.role ? roleName(e.role) : '系统'}
                  {e.tool ? ` / ${e.tool}` : ''}
                </span>
              </div>
              <p>
                {e.target ? `分派给 ${roleName(e.target)} · ` : ''}
                {e.message || e.type}
              </p>
            </li>
          ))}
        </ol>
      ) : (
        <p className="ds-hint">等待运行记录…</p>
      )}
    </div>
  );
}
export function TaskInspector({
  task,
  files,
  sid,
  liveStage,
}: {
  task?: Task;
  files: Dataset[];
  sid: string;
  liveStage?: string;
}) {
  const { message } = App.useApp();
  const query = useQueryClient();
  const choose = useUI((s) => s.choose);
  const draft = useUI((s) => s.drafts[sid] ?? emptyDraft);
  const [busy, setBusy] = useState(false),
    [logOpen, setLogOpen] = useState(false);
  const sources = useQuery({ queryKey: ['sources'], queryFn: ({ signal }) => api.sources(signal) });
  async function cancel() {
    if (!task) return;
    setBusy(true);
    try {
      await api.cancel(task.task_id);
      void message.info('取消请求已受理，正在等待任务确认。');
      await query.invalidateQueries({ queryKey: ['task', task.task_id] });
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function retry() {
    if (!task) return;
    setBusy(true);
    try {
      const next = await api.retry(task.task_id, crypto.randomUUID());
      choose(sid, next.task_id);
      await query.invalidateQueries({ queryKey: ['session', sid] });
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  const metric = task?.telemetry;
  const active = !!task && !terminal.has(task.status);
  // Stream stages ('analysis'/'report') arrive instantly; the polled task.stage
  // covers queued/starting and streaming fallback.
  const stageKey =
    liveStage === 'analysis' || liveStage === 'report' ? liveStage : (task?.stage ?? '');
  const stageText =
    { queued: '排队中', starting: '正在启动', understanding: '理解需求', analysis: '数据分析', report: '生成报告' }[
      stageKey
    ] ?? '';
  const estimate = active ? task?.estimate_seconds ?? null : null;
  const elapsed = metric?.elapsed_seconds ?? null;
  const overrun = estimate != null && elapsed != null && elapsed > estimate;
  const remaining = estimate != null && elapsed != null && !overrun ? estimate - elapsed : null;
  return (
    <div className="ds-task-inspector">
      <div className="ds-section-title">当前任务{task && <Status state={task.status} />}</div>
      {!task ? (
        <p className="ds-hint">提交问题后，这里显示处理阶段、结果和来源。</p>
      ) : (
        <>
          <h3 className="ds-task-question">{task.spec.request}</h3>
          <p className="ds-hint">
            {active ? (
              <>
                {stageText && <Tag color="processing">{stageText}</Tag>}
                <span>
                  已进行 {duration(elapsed)}
                  {remaining != null && ` · 预计还需约 ${duration(remaining)}`}
                  {overrun && ' · 已超过同类任务通常耗时'}
                </span>
              </>
            ) : task.status === 'succeeded' ? (
              task.result?.report_ids?.length
                ? '报告已交付，可在对应回复中打开。'
                : '分析已完成，结论已保存。'
            ) : task.status === 'waiting_user' ? (
              '请在对话中补充信息，系统会关联原任务。'
            ) : (
              '任务记录与已保存结果均保留。'
            )}
          </p>
          {task.error && <ErrorNotice error={task.error} />}
          {active && (
            <Button
              block
              aria-label="取消任务"
              icon={<StopOutlined />}
              loading={busy}
              onClick={cancel}
            >
              取消任务
            </Button>
          )}
          {['failed', 'interrupted', 'cancelled'].includes(task.status) && (
            <Button block icon={<ReloadOutlined />} loading={busy} onClick={retry}>
              重新运行
            </Button>
          )}
          <section className="ds-source-snapshot">
            <h4>本任务已使用的来源</h4>
            {!task.spec.file_ids?.length && !task.spec.database_source_ids?.length ? (
              <p className="ds-hint">本轮未选择数据来源。</p>
            ) : (
              <>
                {task.spec.file_ids?.map((id) => (
                  <p key={id}>
                    <Tag>文件</Tag>
                    {files.find((f) => f.file_id === id)?.name || `已登记文件 ${id.slice(0, 6)}`}
                  </p>
                ))}
                {task.spec.database_source_ids?.map((id) => (
                  <p key={id}>
                    <Tag>SQL</Tag>
                    {sources.data?.items.find((s) => s.source_id === id)?.name ?? id}
                    <small>
                      {task.spec.database_tables?.[id]?.length === 0
                        ? '未选择表（0 张）'
                        : (task.spec.database_tables?.[id]?.join('、') ?? '来源授权范围内的表')}
                    </small>
                  </p>
                ))}
              </>
            )}
          </section>
          <Collapse
            ghost
            items={[
              {
                key: 'usage',
                label: '耗时与 Token',
                children: (
                  <div className="ds-usage">
                    <dl>
                      <div>
                        <dt>响应时间</dt>
                        <dd>{duration(metric?.elapsed_seconds)}</dd>
                      </div>
                      <div>
                        <dt>Token 总量</dt>
                        <dd>{tokens(metric?.total_tokens)}</dd>
                      </div>
                    </dl>
                    <p>
                      输入 {tokens(metric?.input_tokens)} · 输出 {tokens(metric?.output_tokens)}
                    </p>
                    <small>
                      {metric?.usage_status === 'partial'
                        ? '部分调用未返回用量，仅展示已记录部分。'
                        : metric?.usage_status === 'unavailable'
                          ? '模型未返回用量，不按零计算。'
                          : '采用服务商实际返回的用量。'}
                    </small>
                    <p>{metric?.agents?.map(roleName).join('、') || '尚未记录 Agent 分派'}</p>
                  </div>
                ),
              },
            ]}
          />
          <Collapse
            ghost
            destroyOnHidden
            onChange={(keys) => setLogOpen(keys.includes('log'))}
            items={[
              {
                key: 'log',
                label: '运行记录',
                children: logOpen ? <EventLog taskId={task.task_id} active={active} /> : null,
              },
            ]}
          />
        </>
      )}
      <Collapse
        ghost
        items={[
          {
            key: 'next',
            label: '下次提问的数据',
            children: (
              <div className="ds-hint">
                <p>
                  {draft.fileIds.length} 份文件 · {draft.sourceIds?.length ?? 0} 个数据库来源
                </p>
                {draft.fileIds.map((id) => (
                  <p key={id}>{files.find((f) => f.file_id === id)?.name || id.slice(0, 8)}</p>
                ))}
                {draft.sourceIds?.map((id) => (
                  <p key={id}>{id}</p>
                ))}
                <p>此选择仅影响下次提交，不改变上方任务快照。</p>
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
export function ActivityPage({
  session,
  onChoose,
}: {
  session: Session;
  onChoose: (id: string) => void;
}) {
  const [filter, setFilter] = useState('all');
  const logs = session.question_logs ?? [];
  return (
    <div className="ds-page-content">
      <div className="ds-page-heading">
        <div>
          <h2>提问日志</h2>
          <p>每次请求独立记录，响应时间包含排队与执行。</p>
        </div>
        <Select
          aria-label="筛选任务状态"
          value={filter}
          onChange={setFilter}
          options={['all', 'succeeded', 'running', 'failed', 'waiting_user'].map((value) => ({
            value,
            label:
              value === 'all'
                ? '全部状态'
                : (
                    {
                      succeeded: '已完成',
                      running: '执行中',
                      failed: '失败',
                      waiting_user: '待补充',
                    } as Record<string, string>
                  )[value],
          }))}
        />
      </div>
      <Table
        size="middle"
        rowKey="task_id"
        dataSource={logs
          .filter((l) => filter === 'all' || l.status === filter)
          .slice()
          .reverse()}
        pagination={{ pageSize: 20 }}
        scroll={{ x: 760 }}
        columns={[
          {
            title: '用户问题',
            dataIndex: 'question',
            render: (v: string, r) => (
              <Button type="link" className="ds-wrap-button" onClick={() => onChoose(r.task_id)}>
                {v}
              </Button>
            ),
          },
          {
            title: '状态',
            dataIndex: 'status',
            width: 110,
            render: (v: string) => <Status state={v} />,
          },
          {
            title: '响应时间',
            width: 130,
            render: (_, r) => duration(r.telemetry.elapsed_seconds),
          },
          {
            title: 'Token',
            width: 120,
            render: (_, r) => (
              <span>
                {tokens(r.telemetry.total_tokens)}
                {r.telemetry.usage_status === 'partial' && <small className="ds-hint"> 部分</small>}
              </span>
            ),
          },
          { title: '时间', dataIndex: 'created_at', width: 130, render: (v: string) => date(v) },
        ]}
      />
    </div>
  );
}
