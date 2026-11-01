import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../src/shared/api/client';
afterEach(() => vi.unstubAllGlobals());
it('shows the actual FastAPI constraint and request ID instead of a generic HTTP error', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 'file_not_selected',
          message: '当前文件选择不涵盖基础报告的数据',
          request_id: 'r1',
        }),
        { status: 403, headers: { 'Content-Type': 'application/json' } },
      ),
    ),
  );
  await expect(api.session('s1')).rejects.toMatchObject({
    message: '当前文件选择不涵盖基础报告的数据',
    status: 403,
    requestId: 'r1',
  });
});

it('renameSession patches the title and clearSession deletes the messages', async () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push([String(input), init]);
      if (init?.method === 'PATCH') {
        return new Response(
          JSON.stringify({
            session_id: 's1',
            title: '工业分析',
            created_at: '2026-10-31T00:00:00Z',
            messages: [],
            tasks: [],
            question_logs: [],
            report_versions: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return new Response(null, { status: 204 });
    }),
  );
  await api.renameSession('s1', '工业分析');
  await api.clearSession('s1');
  const [patch, clear] = calls;
  expect(patch[0]).toContain('/api/v1/sessions/s1');
  expect(patch[1]?.method).toBe('PATCH');
  expect(JSON.parse(String(patch[1]?.body))).toEqual({ title: '工业分析' });
  expect(clear[0]).toContain('/api/v1/sessions/s1/messages');
  expect(clear[1]?.method).toBe('DELETE');
});
