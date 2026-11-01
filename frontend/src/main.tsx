import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { App as AntApp, ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App } from './app/App';
import { useUI } from './shared/state';
import './styles/index.css';
const client = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 10000, refetchOnWindowFocus: true, gcTime: 300000 },
    mutations: { retry: 0 },
  },
});
function Root() {
  const mode = useUI((s) => s.theme);
  const [reducedMotion, setReducedMotion] = useState(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  );
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReducedMotion(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = mode;
  }, [mode]);
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: mode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          motion: !reducedMotion,
          colorPrimary: mode === 'dark' ? '#8B9FF2' : '#3D5BC7',
          colorText: mode === 'dark' ? '#E4EAF2' : '#26364B',
          colorTextSecondary: mode === 'dark' ? '#A5B1C3' : '#59697F',
          colorTextPlaceholder: mode === 'dark' ? '#A5B1C3' : '#64748B',
          colorBgBase: mode === 'dark' ? '#131820' : '#FFFFFF',
          colorBgContainer: mode === 'dark' ? '#1A222E' : '#FFFFFF',
          colorBgElevated: mode === 'dark' ? '#202A38' : '#FFFFFF',
          colorBorder: mode === 'dark' ? '#405069' : '#D8E0EA',
          colorBorderSecondary: mode === 'dark' ? '#303C4E' : '#E2E8F0',
          colorTextDisabled: mode === 'dark' ? '#8290A5' : '#7B879A',
          colorBgContainerDisabled: mode === 'dark' ? '#202A38' : '#F0F2F5',
          colorSuccess: mode === 'dark' ? '#81C9A7' : '#216747',
          borderRadius: 8,
          fontSize: 14,
          controlHeight: 36,
          fontFamily:
            'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif',
          colorBgLayout: mode === 'dark' ? '#131820' : '#F7F8FA',
        },
        components: {
          Button: { primaryColor: mode === 'dark' ? '#131820' : '#FFFFFF', primaryShadow: 'none' },
          Select: { optionSelectedBg: mode === 'dark' ? '#263249' : '#EDF0FC' },
          Table: { headerBg: mode === 'dark' ? '#1C2430' : '#F7F8FA' },
        },
      }}
    >
      <AntApp>
        <QueryClientProvider client={client}>
          <App />
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  );
}
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
