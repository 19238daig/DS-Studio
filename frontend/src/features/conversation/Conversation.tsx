import { useEffect, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { App, Button, Input, Select, Tag } from 'antd';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CloseOutlined,
  CopyOutlined,
  FileTextOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../../shared/api/client';
import type {
  Dataset,
  Health,
  Session,
  SessionMessage,
  Task,
  Version,
} from '../../shared/api/types';
import { terminal } from '../../shared/api/types';
import { useUI, emptyDraft } from '../../shared/state';
import { date, errorMessage, stageLabels } from '../../shared/ui';
import { ReportAttachment } from '../reports/Reports';

export function conversationMessages(session: Session, task?: Task): SessionMessage[] {
  const messages = [...(session.messages ?? [])];
  for (const version of session.report_versions ?? []) {
    if (!messages.some((m) => m.role === 'assistant' && m.task_id === version.task_id)) {
      const position = messages.findLastIndex((m) => m.task_id === version.task_id);
      const reply = {
        message_id: `report-${version.task_id}`,
        role: 'assistant',
        content: '报告已生成。',
        task_id: version.task_id,
      };
      if (position >= 0) messages.splice(position + 1, 0, reply);
      else messages.push(reply);
    }
  }
  const answer =
    task?.final_response ||
    task?.result?.final_response ||
    (task?.status === 'failed'
      ? `本次任务未完成：${errorMessage(task.error)}`
      : task?.status === 'cancelled'
        ? '任务已取消，可在任务记录中查看处理进度。'
        : task?.status === 'interrupted'
          ? '任务执行中断，可在任务面板重新运行。'
          : '');
  if (task && answer && !messages.some((m) => m.role === 'assistant' && m.task_id === task.task_id))
    messages.push({
      message_id: `final-${task.task_id}`,
      role: 'assistant',
      content: answer,
      task_id: task.task_id,
    });
  return messages;
}
function Message({
  item,
  versions,
  sid,
  failedTaskId,
}: {
  item: SessionMessage;
  versions: Version[];
  sid: string;
  failedTaskId?: string;
}) {
  const user = item.role === 'user';
  const { message } = App.useApp();
  const query = useQueryClient();
  const [retrying, setRetrying] = useState(false);
  async function retry() {
    if (!failedTaskId) return;
    setRetrying(true);
    try {
      const next = await api.retry(failedTaskId, crypto.randomUUID());
      useUI.getState().choose(sid, next.task_id);
      await query.invalidateQueries({ queryKey: ['session', sid] });
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setRetrying(false);
    }
  }
  return (
    <article
      className={'ds-message ' + (user ? 'ds-message-user' : 'ds-message-assistant')}
      data-task-id={item.task_id ?? ''}
    >
      <div className="ds-message-author">
        {user ? (
          '你的问题'
        ) : (
          <>
            <span className="ds-assistant-mark">
              <FileTextOutlined />
            </span>{' '}
            DS 助手
          </>
        )}
        {item.created_at && <span className="ds-message-time">{date(item.created_at)}</span>}
        {!user && item.content && (
          <Button
            aria-label="复制回答"
            type="text"
            size="small"
            icon={<CopyOutlined />}
            onClick={() => {
              navigator.clipboard.writeText(item.content);
              void message.success('回答已复制。');
            }}
          />
        )}
      </div>
      <div className="ds-prose">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.content}</ReactMarkdown>
      </div>
      {failedTaskId && (
        <Button
          aria-label="重新运行本任务"
          size="small"
          icon={<ReloadOutlined />}
          loading={retrying}
          onClick={retry}
        >
          重新运行
        </Button>
      )}
      {!user &&
        versions.map((v) => (
          <ReportAttachment key={v.version ?? v.task_id} sid={sid} version={v} />
        ))}
    </article>
  );
}
export function Conversation({
  session,
  task,
  stream,
  health,
  files,
}: {
  session: Session;
  task?: Task;
  stream: { answer: string; progress: string; fallback: boolean; stage?: string };
  health?: Health;
  files: Dataset[];
}) {
  const live = !!task && !terminal.has(task.status);
  // Stage from the live stream when it is authoritative, else from the polled
  // task record; a per-second local timer keeps elapsed time visibly moving.
  const stageKey =
    stream.stage === 'analysis' || stream.stage === 'report' ? stream.stage : (task?.stage ?? '');
  const stageText = stageLabels[stageKey] ?? '';
  const [elapsed, setElapsed] = useState<number | null>(null);
  useEffect(() => {
    if (!live || !task?.created_at) return;
    const started = Date.parse(task.created_at);
    if (!Number.isFinite(started)) return;
    const update = () => setElapsed(Math.max(0, Math.round((Date.now() - started) / 1000)));
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [live, task?.created_at]);
  const liveElapsed =
    elapsed == null
      ? ''
      : elapsed >= 60
        ? `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒`
        : `${elapsed} 秒`;
  const messages = conversationMessages(session, task);
  const root = useRef<HTMLDivElement>(null);
  const bottom = useRef(true);
  const [newContent, setNewContent] = useState(false);
  const virtual = messages.length > 100;
  const list = useVirtualizer({
    count: virtual ? messages.length : 0,
    getScrollElement: () => root.current,
    estimateSize: () => 190,
    overscan: 6,
    getItemKey: (i) => messages[i]?.message_id ?? i,
  });
  const versionsByTask = new Map<string, Version[]>();
  session.report_versions?.forEach((v) =>
    versionsByTask.set(v.task_id, [...(versionsByTask.get(v.task_id) ?? []), v]),
  );
  const lastReply = new Map<string, number>();
  messages.forEach((m, i) => {
    if (m.role === 'assistant' && m.task_id) lastReply.set(m.task_id, i);
  });
  const scroll = () => {
    const node = root.current;
    if (!node) return;
    if (virtual) list.scrollToIndex(messages.length - 1, { align: 'end' });
    else node.scrollTop = node.scrollHeight;
    setNewContent(false);
    bottom.current = true;
  };
  useEffect(() => {
    bottom.current = true;
    setNewContent(false);
    const id = requestAnimationFrame(scroll);
    return () => cancelAnimationFrame(id);
  }, [session.session_id]);
  useEffect(() => {
    if (bottom.current) {
      const id = requestAnimationFrame(scroll);
      return () => cancelAnimationFrame(id);
    } else setNewContent(true);
  }, [messages.length, stream.answer, stream.progress]);
  const render = (i: number) => {
    const m = messages[i];
    const failedTaskId =
      task?.status === 'failed' && m.message_id === `final-${task.task_id}` ? task.task_id : undefined;
    return (
      <Message
        item={m}
        failedTaskId={failedTaskId}
        sid={session.session_id}
        versions={
          lastReply.get(m.task_id ?? '') === i
            ? (versionsByTask.get(m.task_id ?? '') ?? [])
            : []
        }
      />
    );
  };
  return (
    <div className="ds-conversation">
      <div
        className="ds-chat-scroll"
        ref={root}
        onScroll={() => {
          const el = root.current;
          if (el) bottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
        }}
      >
        {!messages.length ? (
          <Welcome sid={session.session_id} />
        ) : virtual ? (
          <div style={{ height: list.getTotalSize(), position: 'relative' }}>
            {list.getVirtualItems().map((v) => (
              <div
                key={v.key}
                data-index={v.index}
                ref={list.measureElement}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  transform: `translateY(${v.start}px)`,
                }}
              >
                {render(v.index)}
              </div>
            ))}
          </div>
        ) : (
          messages.map((m, i) => <div key={m.message_id}>{render(i)}</div>)
        )}
        {live && (
          <div className="ds-live-message" role="status">
            <span className="ds-live-dot" />
            {stageText && <Tag color="processing">{stageText}</Tag>}
            {liveElapsed && <span>已进行 {liveElapsed}</span>}
            {(task?.stages?.length ?? 0) > 0 && (
              <div className="ds-pipeline" aria-label="任务进度流程">
                {(() => {
                  const stages = task!.stages ?? [];
                  const currentIdx = stages.indexOf(stageKey);
                  return stages.map((s, i) => (
                    <span
                      key={s}
                      className={
                        i === currentIdx
                          ? 'ds-pipeline-active'
                          : i < currentIdx
                            ? 'ds-pipeline-done'
                            : 'ds-pipeline-todo'
                      }
                    >
                      {i > 0 && ' → '}
                      {i < currentIdx ? '✓ ' : ''}
                      {stageLabels[s] ?? s}
                    </span>
                  ));
                })()}
              </div>
            )}
            {stream.progress ||
              (task!.status === 'queued' ? '问题已受理，正在排队。' : '正在分析，请稍候…')}
            {stream.answer && (
              <div className="ds-prose">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{stream.answer}</ReactMarkdown>
              </div>
            )}
            {stream.fallback && <small>实时连接暂不可用，已通过任务状态继续跟踪。</small>}
          </div>
        )}
        {task?.status === 'waiting_user' && (
          <div className="ds-clarification">
            <strong>需要补充信息</strong>
            <p>填写下方输入区继续；将关联原任务和来源。</p>
            <Button
              onClick={() => {
                useUI.getState().patch(session.session_id, {
                  replyTo: task.task_id,
                  base: undefined,
                  fileIds: task.spec.file_ids ?? [],
                  sourceIds: task.spec.database_source_ids ?? [],
                  tables: task.spec.database_tables ?? {},
                  outputs: task.spec.expected_outputs ?? [],
                });
                setTimeout(() =>
                  document.querySelector<HTMLTextAreaElement>('textarea[aria-label="你的问题"]')?.focus(), 80);
              }}
            >
              补充说明
            </Button>
          </div>
        )}
      </div>
      {newContent && (
        <Button className="ds-new-content" icon={<ArrowDownOutlined />} onClick={scroll}>
          有新内容
        </Button>
      )}
      <Composer sid={session.session_id} health={health} files={files} />
    </div>
  );
}
function Welcome({ sid }: { sid: string }) {
  const patch = useUI((s) => s.patch);
  return (
    <div className="ds-welcome">
      <span className="ds-welcome-icon">
        <FileTextOutlined />
      </span>
      <h2>从一个数据问题开始</h2>
      <p>
        选择文件或数据库，查询指标、探索关系，
        <br />
        或把已有结果整理成可继续修改的报告。
      </p>
      <div>
        {[
          '计算所选数据中的平均值，只给结论',
          '分析不同分组的差异，说明关键发现',
          '根据已有分析生成 HTML 报告',
        ].map((q) => (
          <button key={q} onClick={() => patch(sid, { content: q })}>
            {q}
            <ArrowUpOutlined />
          </button>
        ))}
      </div>
      <small>结果与报告会随当前对话保存。</small>
    </div>
  );
}
export function Composer({
  sid,
  health,
  files,
}: {
  sid: string;
  health?: Health;
  files: Dataset[];
}) {
  const draft = useUI((s) => s.drafts[sid] ?? emptyDraft);
  const patch = useUI((s) => s.patch),
    choose = useUI((s) => s.choose);
  const query = useQueryClient();
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const key = useRef<{ body: string; id: string } | null>(null);
  const composing = useRef(false);
  const inFlight = useRef(false);
  async function send() {
    if (
      inFlight.current ||
      !draft.content.trim() ||
      !health?.models_configured ||
      !health?.worker.ready
    )
      return;
    const payload = {
      schema_version: '1.0',
      content: draft.content,
      file_ids: draft.fileIds,
      expected_outputs: draft.outputs,
      ...(draft.sourceIds !== undefined ? { database_source_ids: draft.sourceIds } : {}),
      ...(draft.tables !== undefined ? { database_tables: draft.tables } : {}),
      ...(draft.base ? { base_report_id: draft.base.id } : {}),
      ...(draft.replyTo ? { reply_to_task_id: draft.replyTo } : {}),
    };
    const body = JSON.stringify(payload);
    if (!key.current || key.current.body !== body) key.current = { body, id: crypto.randomUUID() };
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await api.submit(sid, payload, key.current.id);
      choose(sid, result.task_id);
      if (useUI.getState().drafts[sid]?.content === draft.content)
        patch(sid, { content: '', base: undefined, replyTo: undefined, outputs: [] });
      key.current = null;
      setUncertain(false);
      await query.invalidateQueries({ queryKey: ['session', sid] });
      await query.invalidateQueries({ queryKey: ['sessions'] });
    } catch (e) {
      setUncertain(true);
      void message.error(errorMessage(e));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="ds-composer">
      <div className="ds-composer-context">
        <span>下次提问</span>
        <span>
          {draft.fileIds.length} 份文件 · {draft.sourceIds?.length ?? 0} 个数据库来源
        </span>
        <Select
          aria-label="报告输出格式"
          mode="multiple"
          placeholder="需要报告时选择格式"
          value={draft.outputs}
          onChange={(outputs) => patch(sid, { outputs })}
          options={[
            { value: 'html', label: 'HTML 报告' },
            { value: 'xlsx', label: 'Excel 报告' },
          ]}
        />
      </div>
      {draft.base && (
        <div className="ds-revision-banner">
          <Tag color="blue">修改报告</Tag>
          <span>{draft.base.label}</span>
          <Button
            type="text"
            size="small"
            icon={<CloseOutlined />}
            aria-label="退出报告修改"
            onClick={() => patch(sid, { base: undefined })}
          />
        </div>
      )}
      {draft.replyTo && (
        <div className="ds-revision-banner">
          <Tag>补充说明</Tag>关联原任务
          <Button
            type="text"
            size="small"
            aria-label="退出补充说明"
            icon={<CloseOutlined />}
            onClick={() => patch(sid, { replyTo: undefined })}
          />
        </div>
      )}
      {!!draft.fileIds.length && (
        <div className="ds-selected-files">
          {draft.fileIds.map((id) => (
            <Tag
              key={id}
              closable
              onClose={() => patch(sid, { fileIds: draft.fileIds.filter((x) => x !== id) })}
            >
              {files.find((f) => f.file_id === id)?.name || '已登记文件'}
            </Tag>
          ))}
        </div>
      )}
      <div className="ds-input-box">
        <Input.TextArea
          aria-label="你的问题"
          placeholder={
            draft.base
              ? '说明要修改的部分，其余内容将保留…'
              : '输入问题，或说明已有报告需要修改的内容…'
          }
          value={draft.content}
          maxLength={20000}
          autoSize={{ minRows: 2, maxRows: 7 }}
          onChange={(e) => patch(sid, { content: e.target.value })}
          onCompositionStart={() => (composing.current = true)}
          onCompositionEnd={() => (composing.current = false)}
          onKeyDown={(e) => {
            if (
              e.key === 'Enter' &&
              !e.shiftKey &&
              !composing.current &&
              !e.nativeEvent.isComposing &&
              e.nativeEvent.keyCode !== 229
            ) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <Button
          type="primary"
          aria-label="发送问题"
          icon={<ArrowUpOutlined />}
          loading={busy}
          disabled={!draft.content.trim() || !health?.models_configured || !health?.worker.ready}
          onClick={send}
        >
          {busy ? '提交中' : '发送'}
        </Button>
      </div>
      <div className="ds-composer-foot">
        <span>
          {uncertain
            ? '提交结果待确认，再次发送相同内容将复用幂等键。'
            : !health
              ? '正在连接服务…'
              : !health.models_configured
                ? '模型服务未配置，请在本机 .env 中配置。'
                : !health.worker.ready
                  ? 'Worker 未就绪，请检查后台服务。'
                  : draft.content
                  ? `Enter 发送 · ${draft.content.length}/20000`
                  : 'Enter 发送 · Shift + Enter 换行'}
        </span>
        <span>分析结果请结合业务判断</span>
      </div>
    </div>
  );
}
