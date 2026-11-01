import { useEffect, useRef, useState } from 'react';
import { streamURL } from '../../shared/api/client';
import type { StreamEvent } from '../../shared/api/types';
interface StreamState {
  cursor: number;
  answer: string;
  progress: string;
  role: string;
  stage: string;
  connected: boolean;
  fallback: boolean;
  done: boolean;
}
export const initialStream = (): StreamState => ({
  cursor: 0,
  answer: '',
  progress: '',
  role: '',
  stage: '',
  connected: false,
  fallback: false,
  done: false,
});
export function reduceStream(state: StreamState, event: StreamEvent, taskId: string): StreamState {
  if (event.task_id !== taskId || !event.data || typeof event.data !== 'object') return state;
  if (event.seq != null && (!Number.isSafeInteger(event.seq) || event.seq <= state.cursor))
    return state;
  const next = { ...state, cursor: event.seq ?? state.cursor };
  const data = event.data;
  switch (event.kind) {
    case 'status':
    case 'progress':
      return {
        ...next,
        progress: String(data.text ?? ''),
        role: String(event.role ?? ''),
        stage: String(data.stage ?? ''),
      };
    case 'verified_answer_segment':
      return data.verified === true
        ? { ...next, answer: (state.answer + String(data.text ?? '')).slice(0, 65536) }
        : next;
    case 'snapshot':
      return {
        ...next,
        answer: data.verified === true ? String(data.answer ?? '').slice(0, 65536) : '',
      };
    case 'final':
      return { ...next, answer: String(data.answer ?? ''), done: true, connected: false };
    case 'stream_unavailable':
      return { ...next, fallback: true, connected: false };
    default:
      return next;
  }
}
export function useTaskStream(id: string | undefined, enabled: boolean, onFinal: () => void) {
  const [state, setState] = useState(initialStream);
  const scope = useRef(id);
  useEffect(() => {
    scope.current = id;
    setState(initialStream());
    if (!id || !enabled) return;
    let current = initialStream();
    let alive = true;
    let retries = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let connection: EventSource | undefined;
    const connect = () => {
      if (!alive) return;
      connection = new EventSource(streamURL(id, current.cursor));
      connection.onopen = () => {
        if (alive) setState((s) => ({ ...s, connected: true }));
      };
      for (const kind of [
        'status',
        'progress',
        'verified_answer_segment',
        'artifact_ready',
        'snapshot',
        'final',
        'stream_unavailable',
      ]) {
        connection.addEventListener(kind, (event: MessageEvent) => {
          if (!alive) return;
          let parsed: StreamEvent;
          try {
            parsed = JSON.parse(event.data);
          } catch {
            return;
          }
          if (parsed.kind !== kind) return;
          current = reduceStream(current, parsed, id);
          setState(current);
          if (current.done) {
            connection?.close();
            onFinal();
          }
          if (current.fallback) connection?.close();
        });
      }
      connection.onerror = () => {
        connection?.close();
        if (!alive || current.done) return;
        setState((s) => ({ ...s, connected: false }));
        if (++retries > 3) {
          setState((s) => ({ ...s, fallback: true }));
          return;
        }
        timer = setTimeout(connect, Math.min(8000, 1000 * 2 ** retries));
      };
    };
    connect();
    return () => {
      alive = false;
      connection?.close();
      if (timer) clearTimeout(timer);
    };
  }, [id, enabled, onFinal]);
  return scope.current === id ? state : initialStream();
}
