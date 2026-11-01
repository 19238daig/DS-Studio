import { describe, it, expect } from 'vitest';
import { reduceStream, initialStream } from '../src/features/tasks/useTaskStream';
import { conversationMessages } from '../src/features/conversation/Conversation';
import { fileTree } from '../src/features/datasets/Sources';
import type { Session, StreamEvent } from '../src/shared/api/types';
describe('task-scoped stream', () => {
  const event = (
    seq: number,
    kind: string,
    data: StreamEvent['data'],
    task_id = 'a',
  ): StreamEvent => ({ seq, kind, data, task_id });
  it('rejects other tasks, duplicate events and unverified output', () => {
    const start = initialStream();
    expect(
      reduceStream(
        start,
        event(1, 'verified_answer_segment', { text: 'wrong', verified: true }, 'b'),
        'a',
      ),
    ).toBe(start);
    const noText = reduceStream(
      start,
      event(1, 'verified_answer_segment', { text: 'private', verified: false }),
      'a',
    );
    expect(noText.answer).toBe('');
    const next = reduceStream(
      noText,
      event(2, 'verified_answer_segment', { text: 'verified', verified: true }),
      'a',
    );
    expect(next.answer).toBe('verified');
    expect(
      reduceStream(
        next,
        event(2, 'verified_answer_segment', { text: 'duplicate', verified: true }),
        'a',
      ),
    ).toBe(next);
  });
  it('replaces checkpoint rather than duplicating previous text', () => {
    let state = reduceStream(
      initialStream(),
      event(1, 'verified_answer_segment', { text: 'old', verified: true }),
      'a',
    );
    state = reduceStream(state, event(5, 'snapshot', { answer: 'saved', verified: true }), 'a');
    state = reduceStream(
      state,
      event(6, 'verified_answer_segment', { text: ' suffix', verified: true }),
      'a',
    );
    expect(state.answer).toBe('saved suffix');
    expect(reduceStream(state, event(7, 'final', { answer: 'final' }), 'a').done).toBe(true);
  });
  it('connection failure is not task failure', () =>
    expect(reduceStream(initialStream(), event(1, 'stream_unavailable', {}), 'a')).toMatchObject({
      fallback: true,
      done: false,
    }));
  it('keeps the latest progress stage for the phase display', () => {
    let state = reduceStream(
      initialStream(),
      event(1, 'status', { text: '已开始处理', stage: 'agent_assigned' }),
      'a',
    );
    expect(state.stage).toBe('agent_assigned');
    state = reduceStream(
      state,
      {
        ...event(2, 'progress', { text: '正在分析数据', stage: 'analysis' }),
        role: 'analyst',
      },
      'a',
    );
    expect(state.stage).toBe('analysis');
    expect(state.role).toBe('analyst');
  });
});
it('keeps identical report replies attached to their own task', () => {
  const session = {
    session_id: 's',
    title: 'Test',
    created_at: 'now',
    messages: [
      { message_id: 'u1', role: 'user', content: 'a', task_id: 't1' },
      { message_id: 'a1', role: 'assistant', content: '报告已生成。', task_id: 't1' },
      { message_id: 'u2', role: 'user', content: 'b', task_id: 't2' },
    ],
    report_versions: [
      { task_id: 't1', artifacts: [] },
      { task_id: 't2', artifacts: [] },
    ],
  } as unknown as Session;
  const result = conversationMessages(session);
  expect(result.filter((m) => m.role === 'assistant')).toHaveLength(2);
  expect(result.at(-1)?.task_id).toBe('t2');
});
it('constructs folders from KB relative paths while uploads remain isolated', () => {
  const tree = fileTree([
    { file_id: 'a', name: 'sales.csv', size: 1, source: 'kb', relative_path: 'KB/team/sales.csv' },
    {
      file_id: 'b',
      name: 'private.csv',
      size: 1,
      source: 'upload',
      relative_path: 'input/session/private.csv',
    },
  ]);
  expect(tree.map((n) => n.title)).toEqual(['知识库', '会话上传']);
  expect(tree[0].children?.[0].title).toBe('team');
  expect(tree[1].children?.[0].fileId).toBe('b');
});
