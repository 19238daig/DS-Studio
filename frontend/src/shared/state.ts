import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface Draft {
  content: string;
  fileIds: string[];
  outputs: string[];
  sourceIds?: string[];
  tables?: Record<string, string[]>;
  base?: { id: string; label: string };
  replyTo?: string;
}
const fresh = (): Draft => ({ content: '', fileIds: [], outputs: [] });
interface UIState {
  drafts: Record<string, Draft>;
  chosen: Record<string, string>;
  theme: 'light' | 'dark';
  patch: (id: string, change: Partial<Draft>) => void;
  choose: (sid: string, tid: string) => void;
  forget: (sid: string) => void;
  toggleTheme: () => void;
}
export const useUI = create<UIState>()(
  persist(
    (set) => ({
      drafts: {},
      chosen: {},
      theme: localStorage.getItem('ds-theme') === 'dark' ? 'dark' : 'light',
      patch: (id, change) =>
        set((s) => ({ drafts: { ...s.drafts, [id]: { ...(s.drafts[id] ?? fresh()), ...change } } })),
      choose: (sid, tid) => set((s) => ({ chosen: { ...s.chosen, [sid]: tid } })),
      forget: (sid) =>
        set((s) => {
          const drafts = { ...s.drafts },
            chosen = { ...s.chosen };
          delete drafts[sid];
          delete chosen[sid];
          return { drafts, chosen };
        }),
      toggleTheme: () =>
        set((s) => {
          const theme = s.theme === 'light' ? 'dark' : 'light';
          localStorage.setItem('ds-theme', theme);
          return { theme };
        }),
    }),
    {
      name: 'ds-workbench-ui',
      // Only composer drafts survive a reload; task selection and theme keep
      // their own storage paths.
      partialize: (s) => ({ drafts: s.drafts }) as UIState,
    },
  ),
);
export const emptyDraft: Draft = fresh();
