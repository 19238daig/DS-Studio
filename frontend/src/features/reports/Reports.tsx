import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { App, Button, Drawer, Tag } from 'antd';
import {
  DownloadOutlined,
  ExpandOutlined,
  FileExcelOutlined,
  FileTextOutlined,
  EditOutlined,
} from '@ant-design/icons';
import { api, artifactURL } from '../../shared/api/client';
import type { Artifact, Session, Version } from '../../shared/api/types';
import { useUI } from '../../shared/state';
import { Blank, bytes, date, ErrorNotice, errorMessage } from '../../shared/ui';
export function ReportAttachment({ sid, version }: { sid: string; version: Version }) {
  const [preview, setPreview] = useState<string>();
  const patch = useUI((s) => s.patch);
  const navigate = useNavigate();
  const { message } = App.useApp();
  async function revise(artifact: Artifact) {
    try {
      const task = await api.task(version.task_id);
      patch(sid, {
        base: {
          id: artifact.artifact_id,
          label: `版本 ${version.number ?? ''} · ${version.title}`,
        },
        replyTo: undefined,
        fileIds: task.spec.file_ids ?? [],
        sourceIds: task.spec.database_source_ids ?? [],
        tables: task.spec.database_tables ?? {},
        outputs: task.spec.expected_outputs?.length ? task.spec.expected_outputs : ['html'],
      });
      navigate(`/sessions/${sid}/chat`);
      void message.info('已选择基础版本及其来源，请输入修改要求。');
    } catch (e) {
      void message.error(errorMessage(e));
    }
  }
  return (
    <div className="ds-report-attachments">
      {(version.artifacts ?? []).map((a) => (
        <section
          className="ds-report-attachment"
          key={a.artifact_id}
          aria-label={`报告附件 ${version.title}`}
        >
          <div className="ds-report-heading">
            <span className="ds-file-icon">
              {a.mime_type === 'text/html' ? <FileTextOutlined /> : <FileExcelOutlined />}
            </span>
            <div>
              <small>
                {version.revision_of ? '修订报告' : '原始报告'} · 版本 {version.number ?? '—'}
              </small>
              <h3>{version.title || a.name}</h3>
              <span className="ds-hint">
                {a.name} · {bytes(a.size)}
                {version.base_number ? ` · 基于版本 ${version.base_number}` : ''}
              </span>
            </div>
          </div>
          <div className="ds-report-actions">
            {a.downloadable !== false ? (
              <>
                {a.mime_type === 'text/html' && (
                  <Button
                    size="small"
                    onClick={() =>
                      setPreview(preview === a.artifact_id ? undefined : a.artifact_id)
                    }
                  >
                    {preview === a.artifact_id ? '收起预览' : '在对话中预览'}
                  </Button>
                )}
                <Button
                  size="small"
                  icon={<ExpandOutlined />}
                  onClick={() => navigate(`/sessions/${sid}/reports/${a.artifact_id}`)}
                >
                  打开详情
                </Button>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  href={artifactURL(a.artifact_id, 'download')}
                >
                  下载
                </Button>
                <Button size="small" type="text" icon={<EditOutlined />} onClick={() => revise(a)}>
                  基于此版本修改
                </Button>
              </>
            ) : (
              <span>文件暂不可用，请检查任务状态。</span>
            )}
          </div>
          {preview === a.artifact_id && (
            <iframe
              title={`${version.title} 对话内预览`}
              src={artifactURL(a.artifact_id, 'preview')}
              sandbox="allow-scripts allow-downloads"
              className="ds-inline-report"
            />
          )}
        </section>
      ))}
    </div>
  );
}
export function ReportsPage({ session }: { session: Session }) {
  const versions = session.report_versions ?? [];
  return (
    <div className="ds-page-content">
      <div className="ds-page-heading">
        <div>
          <h2>文件与报告</h2>
          <p>按版本保存，每次修改都保留原文件。</p>
        </div>
        <Tag>{versions.length} 个版本</Tag>
      </div>
      {versions.length ? (
        versions.map((v) => (
          <div key={v.version ?? v.task_id} className="ds-report-version">
            <small className="ds-hint">{date(String(v.created_at ?? ''))}</small>
            <ReportAttachment sid={session.session_id} version={v} />
          </div>
        ))
      ) : (
        <Blank title="还没有生成报告">
          <Link to={`/sessions/${session.session_id}/chat`}>回到对话，提出报告需求</Link>
        </Blank>
      )}
      <h3>任务中间产物</h3>
      <p className="ds-hint">脚本、完整结果与日志由所属任务保存。选择任务后按需查看。</p>
      <TaskArtifacts sid={session.session_id} taskIds={session.tasks ?? []} />
    </div>
  );
}
function TaskArtifacts({ sid, taskIds }: { sid: string; taskIds: string[] }) {
  const [selected, setSelected] = useState('');
  const query = useQuery({
    queryKey: ['artifacts', selected],
    queryFn: ({ signal }) => api.artifacts(selected, signal),
    enabled: !!selected,
  });
  return (
    <>
      <select
        aria-label="查看哪个任务的中间产物"
        className="ds-native-select"
        value={selected}
        onChange={(e) => setSelected(e.target.value)}
      >
        <option value="">选择任务</option>
        {[...taskIds].reverse().map((tid, i) => (
          <option key={tid} value={tid}>
            任务 {taskIds.length - i} · {tid.slice(0, 8)}
          </option>
        ))}
      </select>
      {query.error && <ErrorNotice error={query.error} />}
      {query.data?.items
        .filter((a) => a.kind !== 'report')
        .map((a) => (
          <div className="ds-artifact-row" key={a.artifact_id}>
            <span>
              {a.name}
              <small>
                {a.kind} · {bytes(a.size)}
              </small>
            </span>
            <Button
              size="small"
              href={a.downloadable ? artifactURL(a.artifact_id, 'download') : undefined}
              disabled={!a.downloadable}
              icon={<DownloadOutlined />}
            >
              下载
            </Button>
          </div>
        ))}
    </>
  );
}
export function ReportViewer({
  sid,
  artifactId,
  session,
}: {
  sid: string;
  artifactId: string;
  session: Session;
}) {
  const navigate = useNavigate();
  const a = session.report_versions
    ?.flatMap((v) => v.artifacts ?? [])
    .find((a) => a.artifact_id === artifactId);
  return (
    <Drawer
      title={a?.name ?? '报告详情'}
      open
      onClose={() => navigate(`/sessions/${sid}/chat`)}
      size="large"
      className="ds-report-drawer"
      extra={
        a && (
          <Button icon={<DownloadOutlined />} href={artifactURL(a.artifact_id, 'download')}>
            下载
          </Button>
        )
      }
    >
      {!a ? (
        <Blank title="该报告不属于当前会话或已不可用" />
      ) : a.mime_type === 'text/html' ? (
        <iframe
          title="完整报告预览"
          src={artifactURL(a.artifact_id, 'preview')}
          sandbox="allow-scripts allow-downloads"
          className="ds-full-report"
        />
      ) : (
        <Blank title="Excel 工作簿请下载后查看">
          <Button href={artifactURL(a.artifact_id, 'download')}>下载 Excel</Button>
        </Blank>
      )}
    </Drawer>
  );
}
