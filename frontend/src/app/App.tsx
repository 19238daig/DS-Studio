import { Component, lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
} from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  App as AntApp,
  Alert,
  Badge,
  Button,
  Drawer,
  Grid,
  Dropdown,
  Input,
  Modal,
  Select,
  Spin,
  Tooltip,
} from 'antd';
import {
  BarChartOutlined,
  PlusOutlined,
  DeleteOutlined,
  MenuFoldOutlined,
  SearchOutlined,
  DatabaseOutlined,
  InfoCircleOutlined,
  MoonOutlined,
  SunOutlined,
  ReloadOutlined,
  MoreOutlined,
  EditOutlined,
  ClearOutlined,
} from '@ant-design/icons';
import { api } from '../shared/api/client';
import type { Health, Session } from '../shared/api/types';
import { terminal } from '../shared/api/types';
import { emptyDraft, useUI } from '../shared/state';
import { Blank, ErrorNotice, errorMessage, Loading } from '../shared/ui';
import { SourcePicker, useFiles } from '../features/datasets/Sources';
import { TaskInspector, ActivityPage } from '../features/tasks/Tasks';
import { Conversation } from '../features/conversation/Conversation';
import { ReportsPage, ReportViewer } from '../features/reports/Reports';
import { useTaskStream } from '../features/tasks/useTaskStream';
import styles from './App.module.css';
const Memory = lazy(() => import('../features/memory/Memory'));

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    return this.state.error ? (
      <div className="ds-fatal">
        <h1>页面暂时无法显示</h1>
        <p>已保存的任务与报告仍在后端。重新加载页面恢复连接。</p>
        <Button onClick={() => location.reload()}>重新加载</Button>
      </div>
    ) : (
      this.props.children
    );
  }
}
export function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/sessions/:sessionId/:page" element={<Workbench />} />
          <Route path="/sessions/:sessionId/reports/:artifactId" element={<Workbench />} />
          <Route
            path="*"
            element={
              <Blank title="页面不存在">
                <Link to="/">返回工作台</Link>
              </Blank>
            }
          />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
function useSessions() {
  return useQuery({
    queryKey: ['sessions'],
    queryFn: ({ signal }) => api.sessions(signal),
    staleTime: 30000,
  });
}
function Home() {
  const sessions = useSessions();
  const navigate = useNavigate();
  const query = useQueryClient();
  const [busy, setBusy] = useState(false);
  const { message } = AntApp.useApp();
  async function create() {
    setBusy(true);
    try {
      const s = await api.create();
      await query.invalidateQueries({ queryKey: ['sessions'] });
      navigate(`/sessions/${s.session_id}/chat`);
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  if (sessions.isPending) return <Loading />;
  if (sessions.error)
    return (
      <div className="ds-fatal">
        <ErrorNotice error={sessions.error} retry={() => void sessions.refetch()} />
      </div>
    );
  if (sessions.data?.items[0])
    return <Navigate replace to={`/sessions/${sessions.data.items[0].session_id}/chat`} />;
  return (
    <div className="ds-onboarding">
      <span className="ds-brand-mark">
        <BarChartOutlined />
      </span>
      <h1>DS Studio</h1>
      <p>
        让数据问题成为清晰结论。
        <br />
        建立第一个工作空间，选择你的数据开始分析。
      </p>
      <Button type="primary" size="large" icon={<PlusOutlined />} loading={busy} onClick={create}>
        创建第一个对话
      </Button>
    </div>
  );
}
const tabs = [
  { key: 'chat', label: '数据对话' },
  { key: 'data', label: '数据与知识库' },
  { key: 'reports', label: '文件与报告' },
  { key: 'activity', label: '提问日志' },
  { key: 'memory', label: 'Agent 记忆' },
];
function Workbench() {
  const { sessionId: sid = '', page, artifactId } = useParams();
  const screens = Grid.useBreakpoint();
  const isDesktop = screens.lg !== false;
  const view = artifactId ? 'reports' : (page ?? 'chat');
  const navigate = useNavigate(),
    query = useQueryClient();
  const { message } = AntApp.useApp();
  const sessions = useSessions();
  const session = useQuery({
    queryKey: ['session', sid],
    queryFn: ({ signal }) => api.session(sid, signal),
    enabled: !!sid,
  });
  const health = useQuery({
    queryKey: ['health'],
    queryFn: ({ signal }) => api.health(signal),
    refetchInterval: 10000,
  });
  const { files } = useFiles(sid);
  const theme = useUI((s) => s.theme),
    toggle = useUI((s) => s.toggleTheme),
    choose = useUI((s) => s.choose);
  const chosen = useUI((s) => s.chosen[sid]);
  const streamConnectedRef = useRef(false);
    const pending = session.data?.question_logs?.findLast((l) => !terminal.has(l.status))?.task_id;
  const tid = chosen ?? pending ?? session.data?.question_logs?.at(-1)?.task_id;
  const task = useQuery({
    queryKey: ['task', tid],
    queryFn: ({ signal }) => api.task(tid!, signal),
    enabled: !!tid,
    refetchInterval: (q) =>
      q.state.data && terminal.has(q.state.data.status)
        ? false
        : streamConnectedRef.current
          ? 10000
          : 3000,
  });
  const onFinal = useCallback(() => {
    void query.invalidateQueries({ queryKey: ['task', tid] });
    void query.invalidateQueries({ queryKey: ['session', sid] });
    void query.invalidateQueries({ queryKey: ['sessions'] });
  }, [query, tid, sid]);
  const stream = useTaskStream(
    tid,
    !!(tid && task.data && !terminal.has(task.data.status) && health.data?.streaming.enabled),
    onFinal,
  );
  streamConnectedRef.current = stream.connected;
  useEffect(() => {
    if (task.data && terminal.has(task.data.status)) {
      void query.invalidateQueries({ queryKey: ['session', sid] });
      void query.invalidateQueries({ queryKey: ['files', sid] });
      void query.invalidateQueries({ queryKey: ['kb'] });
    }
  }, [sid, tid, task.data?.status, query]);
  const [left, setLeft] = useState(false),
    [right, setRight] = useState(false),
    [filter, setFilter] = useState(''),
    [deleting, setDeleting] = useState(false),
    [actionBusy, setActionBusy] = useState(false),
    [confirmAction, setConfirmAction] = useState<{ action: 'delete' | 'clear'; id: string }>(),
    [renaming, setRenaming] = useState<{ id: string; title: string }>();
  useEffect(() => {
    const resize = () => {
      if (window.innerWidth >= 1024) setLeft(false);
      if (window.innerWidth >= 1280) setRight(false);
    };
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);
  async function create() {
    try {
      const next = await api.create();
      await query.invalidateQueries({ queryKey: ['sessions'] });
      navigate(`/sessions/${next.session_id}/chat`);
      setLeft(false);
    } catch (e) {
      void message.error(errorMessage(e));
    }
  }
  async function remove(id: string) {
    setActionBusy(true);
    try {
      await api.removeSession(id);
      useUI.getState().forget(id);
      query.removeQueries({ queryKey: ['session', id] });
      query.removeQueries({ queryKey: ['files', id] });
      await query.invalidateQueries({ queryKey: ['sessions'] });
      setDeleting(false);
      setConfirmAction(undefined);
      if (id === sid) navigate('/');
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setActionBusy(false);
    }
  }
  async function clear(id: string) {
    setActionBusy(true);
    try {
      await api.clearSession(id);
      useUI.getState().forget(id);
      query.removeQueries({ queryKey: ['session', id] });
      await query.invalidateQueries({ queryKey: ['sessions'] });
      await query.invalidateQueries({ queryKey: ['task'] });
      setConfirmAction(undefined);
      void message.success('对话记录已清空，上传的文件保留。');
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setActionBusy(false);
    }
  }
  async function rename() {
    if (!renaming) return;
    const title = renaming.title.trim();
    if (!title) {
      void message.error('会话标题不能为空。');
      return;
    }
    setActionBusy(true);
    try {
      await api.renameSession(renaming.id, title);
      await query.invalidateQueries({ queryKey: ['sessions'] });
      if (renaming.id === sid) void query.invalidateQueries({ queryKey: ['session', sid] });
      setRenaming(undefined);
      void message.success('会话已重命名。');
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setActionBusy(false);
    }
  }
  const side = (
    <>
      <div className={styles.brand}>
        <span className="ds-brand-mark">
          <BarChartOutlined />
        </span>
        <div>
          <strong>DS Studio</strong>
          <small>数据分析工作台</small>
        </div>
      </div>
      <Button type="primary" block icon={<PlusOutlined />} onClick={create}>
        新建对话
      </Button>
      <div className="ds-section-title ds-recent-label">最近会话</div>
      <Input
        aria-label="搜索会话"
        placeholder="搜索对话"
        prefix={<SearchOutlined />}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        allowClear
      />
      <nav className={styles.sessionList} aria-label="最近会话">
        {sessions.data?.items
          .filter((s) => s.title.toLowerCase().includes(filter.toLowerCase()))
          .map((s) => (
            <div
              key={s.session_id}
              className={`${styles.sessionItem} ${s.session_id === sid ? styles.activeSession : ''}`}
            >
              <Link to={`/sessions/${s.session_id}/chat`} onClick={() => setLeft(false)}>
                <span className={styles.sessionDot} />
                <span>{s.title || '新会话'}</span>
              </Link>
              <Dropdown
                trigger={['click']}
                menu={{
                  items: [
                    { key: 'rename', icon: <EditOutlined />, label: '重命名' },
                    { key: 'clear', icon: <ClearOutlined />, label: '清空记录' },
                    { key: 'delete', icon: <DeleteOutlined />, label: '删除对话', danger: true },
                  ],
                  onClick: ({ key }) => {
                    if (key === 'rename') setRenaming({ id: s.session_id, title: s.title || '' });
                    else setConfirmAction({ action: key as 'clear' | 'delete', id: s.session_id });
                  },
                }}
              >
                <Button
                  aria-label={`会话操作：${s.title || '新会话'}`}
                  className={styles.sessionActions}
                  type="text"
                  size="small"
                  icon={<MoreOutlined />}
                />
              </Dropdown>
            </div>
          ))}
      </nav>
      <div className={styles.sources}>
        <SourcePicker key={sid} sid={sid} health={health.data} />
      </div>
    </>
  );
  const inspector = (
    <>
      <div className={styles.inspectorHead}>
        <h2>任务状态</h2>
        {session.data?.question_logs?.length ? (
          <Select
            aria-label="选择任务记录"
            size="small"
            className="ds-full"
            value={tid}
            onChange={(id) => choose(sid, id)}
            options={[...(session.data?.question_logs ?? [])]
              .reverse()
              .map((l) => ({ value: l.task_id, label: l.question }))}
          />
        ) : null}
      </div>
      {task.error && <ErrorNotice error={task.error} retry={() => void task.refetch()} />}
      <TaskInspector key={tid ?? sid} task={task.data} files={files} sid={sid} liveStage={stream.stage} />
    </>
  );
  return (
    <div className={styles.shell}>
      {isDesktop && <aside className={styles.sidebar}>{side}</aside>}
      <main className={styles.main}>
        <header className={styles.topbar}>
          <div className={styles.breadcrumb}>
            <Button
              className="ds-mobile-nav"
              aria-label="打开会话与数据"
              icon={<MenuFoldOutlined />}
              onClick={() => setLeft(true)}
            />
            <span>工作空间</span>
            <span>/</span>
            <strong>{session.data?.title || '正在载入…'}</strong>
          </div>
          <div className={styles.topActions}>
            <Badge
              status={health.error ? 'error' : health.data?.worker.ready ? 'success' : 'warning'}
              text={
                health.error
                  ? '服务连接中断'
                  : health.data?.worker.ready
                    ? '分析服务已就绪'
                    : '等待 Worker'
              }
            />
            <Tooltip title="重新读取会话">
              <Button
                aria-label="刷新当前会话"
                type="text"
                icon={<ReloadOutlined />}
                onClick={() => {
                  void session.refetch();
                  if (tid) void task.refetch();
                }}
              />
            </Tooltip>
            <Button
              aria-label={theme === 'light' ? '切换深色主题' : '切换浅色主题'}
              type="text"
              icon={theme === 'light' ? <MoonOutlined /> : <SunOutlined />}
              onClick={toggle}
            />
            <Button
              aria-label="删除当前会话"
              type="text"
              icon={<DeleteOutlined />}
              onClick={() => setDeleting(true)}
            />
          </div>
        </header>
        <div className={styles.heading}>
          <div>
            <h1>{tabs.find((t) => t.key === view)?.label ?? '数据对话'}</h1>
            <p>查询指标、探索数据，或继续修改已有报告。</p>
          </div>
          <Button
            aria-label="任务与来源"
            className="ds-inspector-toggle"
            icon={<InfoCircleOutlined />}
            onClick={() => setRight(true)}
          >
            任务与来源
          </Button>
        </div>
        <nav className={styles.tabs} aria-label="工作台导航">
          {tabs.map((t) => (
            <Link
              key={t.key}
              className={view === t.key ? styles.activeTab : ''}
              to={`/sessions/${sid}/${t.key}`}
            >
              {t.label}
            </Link>
          ))}
        </nav>
        {health.error && (
          <Alert type="warning" showIcon title="后端连接暂不可用，已保存的数据不会被清除。" />
        )}
        <div className={styles.workspace}>
          <section className={styles.content} aria-label="当前工作区">
            {session.isPending ? (
              <Loading />
            ) : session.error ? (
              <ErrorNotice error={session.error} retry={() => void session.refetch()} />
            ) : session.data ? (
              <>
                {view === 'chat' ? (
                  <>
                    <div className={styles.conversationTitle}>
                      <strong>当前对话</strong>
                      <small>对话与结果自动保存</small>
                    </div>
                    <Conversation
                      session={session.data}
                      task={task.data}
                      stream={stream}
                      health={health.data}
                      files={files}
                    />
                  </>
                ) : view === 'data' ? (
                  <div className="ds-page-content">
                    <div className="ds-page-heading">
                      <div>
                        <h2>数据与知识库</h2>
                        <p>选择来源、检查字段和画像，再发起分析。</p>
                      </div>
                      <DatabaseOutlined />
                    </div>
                    <SourcePicker key={sid} sid={sid} health={health.data} expanded />
                  </div>
                ) : view === 'reports' ? (
                  <ReportsPage session={session.data} />
                ) : view === 'activity' ? (
                  <ActivityPage
                    session={session.data}
                    onChoose={(id) => {
                      choose(sid, id);
                      setRight(true);
                    }}
                  />
                ) : view === 'memory' ? (
                  <Suspense fallback={<Loading />}>
                    <Memory />
                  </Suspense>
                ) : (
                  <Blank title="页面不存在" />
                )}
                {artifactId && (
                  <ReportViewer sid={sid} artifactId={artifactId} session={session.data} />
                )}
              </>
            ) : null}
          </section>
          <aside className={styles.inspector} aria-label="当前任务检查面板">
            {inspector}
          </aside>
        </div>
      </main>
      <Drawer
        title="会话与数据"
        placement="left"
        open={left}
        onClose={() => setLeft(false)}
        size={320}
        className="ds-side-drawer"
      >
        {side}
      </Drawer>
      <Drawer title="任务与来源" open={right} onClose={() => setRight(false)} size={360}>
        {inspector}
      </Drawer>
      <Modal
        title="删除当前会话？"
        open={deleting}
        onCancel={() => setDeleting(false)}
        onOk={() => remove(sid)}
        confirmLoading={actionBusy}
        okText="删除会话"
        cancelText="保留"
        okButtonProps={{ danger: true }}
      >
        <p>
          将删除本会话的消息、任务与产物，知识库和其他会话不受影响。正在执行的任务请先取消并等待结束。
        </p>
      </Modal>
      <Modal
        title={confirmAction?.action === 'clear' ? '清空对话记录？' : '删除对话？'}
        open={!!confirmAction}
        onCancel={() => setConfirmAction(undefined)}
        onOk={() => {
          if (!confirmAction) return;
          return confirmAction.action === 'clear' ? clear(confirmAction.id) : remove(confirmAction.id);
        }}
        confirmLoading={actionBusy}
        okText={confirmAction?.action === 'clear' ? '清空记录' : '删除对话'}
        cancelText="取消"
        okButtonProps={{ danger: true }}
      >
        {confirmAction?.action === 'clear' ? (
          <p>将删除该会话的聊天记录、任务与生成的报告；已上传的文件和数据源保留，可以继续提问。</p>
        ) : (
          <p>
            将删除该会话的全部消息、任务、报告和上传文件；知识库与其他会话不受影响。正在执行的任务请先取消并等待结束。
          </p>
        )}
      </Modal>
      <Modal
        title="重命名会话"
        open={!!renaming}
        onCancel={() => setRenaming(undefined)}
        onOk={rename}
        confirmLoading={actionBusy}
        okText="保存"
        cancelText="取消"
      >
        <Input
          aria-label="会话名称"
          value={renaming?.title ?? ''}
          maxLength={200}
          onChange={(e) => setRenaming((r) => (r ? { ...r, title: e.target.value } : r))}
          onPressEnter={rename}
          autoFocus
        />
      </Modal>
    </div>
  );
}
