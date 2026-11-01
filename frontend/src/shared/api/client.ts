import type { components } from './generated';
import type {
  Session,
  Task,
  Artifact,
  Dataset,
  Source,
  Catalog,
  Health,
  EventRecord,
  DatabasePreview,
} from './types';
export class ApiError extends Error {
  constructor(
    message: string,
    public status = 0,
    public requestId?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
const base = import.meta.env.VITE_API_BASE || '/api/v1';
export const artifactURL = (id: string, mode: 'preview' | 'download') =>
  `${base}/artifacts/${encodeURIComponent(id)}/${mode}`;
async function read<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(base + path, options);
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    throw new ApiError('无法连接服务，请检查 FastAPI 是否运行。');
  }
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      /* non-JSON gateway error */
    }
    const error = data?.error ?? data?.detail;
    const message =
      typeof data?.message === 'string'
        ? data.message
        : typeof error?.message === 'string'
          ? error.message
          : typeof error === 'string'
            ? error
            : `请求未完成（HTTP ${response.status}）`;
    throw new ApiError(message, response.status, data?.request_id);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}
const write = <T>(path: string, data: unknown = {}, key?: string) =>
  read<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) },
    body: JSON.stringify(data),
  });
export const api = {
  health: (signal?: AbortSignal) => read<Health>('/health', { signal }),
  sessions: (signal?: AbortSignal) =>
    read<{ items: Session[] }>('/sessions?summary=true', { signal }),
  session: (id: string, signal?: AbortSignal) => read<Session>(`/sessions/${id}`, { signal }),
  create: () => write<Session>('/sessions'),
  removeSession: (id: string) => read<void>(`/sessions/${id}`, { method: 'DELETE' }),
  renameSession: (id: string, title: string) =>
    read<Session>(`/sessions/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }),
  clearSession: (id: string) => read<void>(`/sessions/${id}/messages`, { method: 'DELETE' }),
  files: (id: string, signal?: AbortSignal) =>
    read<{ items: Dataset[] }>(`/sessions/${id}/files`, { signal }),
  kb: (signal?: AbortSignal) => read<{ items: Dataset[] }>('/kb/files', { signal }),
  refreshKB: () => write<{ task_id: string }>('/kb/refresh'),
  task: (id: string, signal?: AbortSignal) => read<Task>(`/tasks/${id}`, { signal }),
  submit: (id: string, payload: components['schemas']['MessageRequest'], key: string) =>
    write<{ task_id: string }>(`/sessions/${id}/messages`, payload, key),
  cancel: (id: string) => write<Task>(`/tasks/${id}/cancel`),
  retry: (id: string, key: string) => write<Task>(`/tasks/${id}/retry`, {}, key),
  artifacts: (id: string, signal?: AbortSignal) =>
    read<{ items: Artifact[] }>(`/tasks/${id}/artifacts`, { signal }),
  events: (id: string, after = 0, signal?: AbortSignal) =>
    read<{ items: EventRecord[]; cursor: number }>(`/tasks/${id}/events?after=${after}`, {
      signal,
    }),
  sources: (signal?: AbortSignal) => read<{ items: Source[] }>('/database/sources', { signal }),
  catalog: (id: string, signal?: AbortSignal) =>
    read<Catalog>(`/database/sources/${id}/catalog`, { signal }),
  databasePreview: (sid: string, id: string, table: string) =>
    write<DatabasePreview>(`/sessions/${sid}/database/preview`, {
      source_id: id,
      table,
      limit: 20,
    }),
  memories: (role: string) =>
    read<{
      items: {
        memory_id: string;
        content: string;
        kind: string;
        status: string;
        created_at: string;
      }[];
    }>(`/memories/${role}`),
  deleteMemory: (role: string, id: string) =>
    read<void>(`/memories/${role}/${id}`, { method: 'DELETE' }),
};
export function uploadFiles(
  sid: string,
  files: File[],
  onProgress: (p: number) => void,
  signal: AbortSignal,
): Promise<{ items: Dataset[] }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const data = new FormData();
    files.forEach((file) => data.append('files', file));
    const abort = () => xhr.abort();
    signal.addEventListener('abort', abort, { once: true });
    xhr.open('POST', `${base}/sessions/${sid}/files`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.min(100, Math.floor((e.loaded / e.total) * 100)));
    };
    xhr.onload = () => {
      signal.removeEventListener('abort', abort);
      let result;
      try {
        result = JSON.parse(xhr.responseText);
      } catch {
        reject(new ApiError('上传服务返回无效响应。', xhr.status));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else
        reject(
          new ApiError(
            result?.message || result?.error?.message || '上传失败，请核对文件类型和容量。',
            xhr.status,
            result?.request_id,
          ),
        );
    };
    xhr.onerror = () => {
      signal.removeEventListener('abort', abort);
      reject(new ApiError('上传连接中断，请检查服务后明确重试。'));
    };
    xhr.onabort = () => {
      signal.removeEventListener('abort', abort);
      reject(new DOMException('上传已取消', 'AbortError'));
    };
    if (signal.aborted) {
      signal.removeEventListener('abort', abort);
      reject(new DOMException('上传已取消', 'AbortError'));
      return;
    }
    xhr.send(data);
  });
}
export const streamURL = (id: string, after: number) => `${base}/tasks/${id}/stream?after=${after}`;
