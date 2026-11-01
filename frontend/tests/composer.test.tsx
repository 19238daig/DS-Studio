import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App as AntApp, ConfigProvider } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Composer } from '../src/features/conversation/Conversation';
import { api } from '../src/shared/api/client';
import { useUI } from '../src/shared/state';
import type { Health } from '../src/shared/api/types';
const health: Health = {
  status: 'ok',
  models_configured: true,
  worker: { ready: true },
  streaming: { enabled: true },
  database: { configured: false },
  limits: {},
};
function mount(h = health) {
  return render(
    <ConfigProvider>
      <AntApp>
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <Composer sid="s1" health={h} files={[]} />
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>,
  );
}
beforeEach(() =>
  useUI.setState({
    drafts: {
      s1: { content: '计算平均收入', fileIds: [], outputs: [], sourceIds: [], tables: {} },
    },
    chosen: {},
  }),
);
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
describe('submission boundary', () => {
  it('blocks keyboard submission while the Worker is unavailable', () => {
    const submit = vi.spyOn(api, 'submit');
    mount({ ...health, worker: { ready: false } });
    fireEvent.keyDown(screen.getByRole('textbox', { name: '你的问题' }), { key: 'Enter' });
    expect(submit).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '发送问题' })).toBeDisabled();
  });
  it('keeps one in-flight request, preserves explicit empty selection and reuses the key after uncertain failure', async () => {
    let reject: (e: Error) => void = () => {};
    const submit = vi
      .spyOn(api, 'submit')
      .mockImplementationOnce(
        () =>
          new Promise((_, r) => {
            reject = r;
          }),
      )
      .mockResolvedValue({ task_id: 't1' });
    mount();
    const input = screen.getByRole('textbox', { name: '你的问题' });
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(submit).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error('network disconnected')));
    await waitFor(() => expect(screen.getByRole('button', { name: '发送问题' })).toBeEnabled());
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[0][1]).toMatchObject({
      file_ids: [],
      database_source_ids: [],
      database_tables: {},
    });
    expect(submit.mock.calls[0][2]).toBe(submit.mock.calls[1][2]);
    await waitFor(() => expect(input).toHaveValue(''));
  });
});
