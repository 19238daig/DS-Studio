/** Explicit read-only check of the locally running production build. */
import { expect, test } from '@playwright/test';
test('production workbench loads existing sessions and data with no task creation', async ({
  page,
}, info) => {
  const errors: string[] = [];
  const mutations: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('request', (r) => {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(r.method()))
      mutations.push(r.method() + ' ' + new URL(r.url()).pathname);
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '数据对话', level: 1 })).toBeVisible();
  await expect(
    page.getByRole('navigation', { name: '最近会话' }).getByRole('link').first(),
  ).toBeVisible();
  await expect(page.getByRole('textbox', { name: '你的问题', exact: true })).toBeInViewport();
  await page.screenshot({ path: info.outputPath('production-chat.png'), fullPage: true });
  await page.getByRole('link', { name: '文件与报告', exact: true }).click();
  await expect(page.getByRole('heading', { name: '文件与报告', level: 1 })).toBeVisible();
  await page.getByRole('link', { name: '数据与知识库', exact: true }).click();
  await expect(page.getByRole('heading', { name: '数据与知识库', level: 1 })).toBeVisible();
  await page.getByRole('link', { name: 'Agent 记忆', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Agent 记忆', level: 1 })).toBeVisible();
  await expect(page.getByRole('combobox', { name: '选择专家记忆' })).toBeVisible();
  expect(errors).toEqual([]);
  expect(mutations).toEqual([]);
});
