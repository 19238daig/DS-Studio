import { Alert, Button, Empty, Skeleton } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { ReactNode } from 'react';
import { ApiError } from './api/client';
export const roleName = (role: string) =>
  ({ main: '主 Agent', analyst: '数据分析 Agent', report_designer: '报告生成 Agent' })[role] ??
  role;
export const stateName = (state: string) =>
  ({
    queued: '排队中',
    running: '执行中',
    succeeded: '已完成',
    failed: '执行失败',
    waiting_user: '需要补充信息',
    cancelled: '已取消',
    interrupted: '执行中断',
  })[state] ?? state;
export function Status({ state }: { state: string }) {
  return <span className={`ds-status ds-status-${state}`}>{stateName(state)}</span>;
}
export const bytes = (n: number) =>
  n >= 1024 ** 3
    ? `${(n / 1024 ** 3).toFixed(2)} GiB`
    : n >= 1024 ** 2
      ? `${(n / 1024 ** 2).toFixed(1)} MB`
      : `${Math.ceil(n / 1024)} KB`;
export const tokens = (n: number | null | undefined) =>
  n == null ? '未返回' : new Intl.NumberFormat('zh-CN').format(n);
export const duration = (n: number | null | undefined) =>
  n == null
    ? '—'
    : n < 60
      ? `${n.toFixed(2)} 秒`
      : `${Math.floor(n / 60)} 分 ${(n % 60).toFixed(1)} 秒`;
export const date = (s?: string | null) =>
  s
    ? new Date(s).toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—';
export function errorMessage(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value instanceof Error) return value.message;
  if (value && typeof value === 'object' && 'message' in value) return String(value.message);
  return '操作未完成，请检查服务和输入。';
}
export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <Alert
      type="error"
      showIcon
      title={errorMessage(error)}
      description={
        error instanceof ApiError && error.requestId ? `请求编号 ${error.requestId}` : undefined
      }
      action={
        retry ? (
          <Button onClick={retry} size="small" icon={<ReloadOutlined />}>
            重试读取
          </Button>
        ) : undefined
      }
    />
  );
}
export function Blank({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="blank">
      <Empty description={title} image={Empty.PRESENTED_IMAGE_SIMPLE} />
      {children}
    </div>
  );
}
export function Loading() {
  return (
    <div className="loading">
      <Skeleton active paragraph={{ rows: 4 }} />
    </div>
  );
}
