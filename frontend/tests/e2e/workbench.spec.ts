import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

test('1000-message history uses a bounded DOM and keeps the composer responsive', async ({
  page,
}, info) => {
  const sessions = await (await page.request.get('/api/v1/sessions?summary=true')).json();
  const long = sessions.items.find((s: { title: string }) => s.title === '长会话性能验收');
  await page.goto(`/sessions/${long.session_id}/chat`);
  const input = page.getByRole('textbox', { name: '你的问题', exact: true });
  await expect(input).toBeInViewport();
  await expect(page.locator('.ds-message').first()).toBeVisible();
  expect(await page.locator('.ds-message').count()).toBeLessThan(80);
  const start = await page.evaluate(() => performance.now());
  await input.fill('长会话中的输入仍应顺畅');
  await expect(input).toHaveValue('长会话中的输入仍应顺畅');
  const elapsed = (await page.evaluate(() => performance.now())) - start;
  expect(elapsed).toBeLessThan(1000);
  await info.attach('input-latency', {
    body: JSON.stringify({
      messages: 1000,
      rendered: await page.locator('.ds-message').count(),
      input_ms: elapsed,
    }),
    contentType: 'application/json',
  });
  await page.screenshot({ path: info.outputPath('long-history.png'), fullPage: true });
});

test('real API: report attachments, independent scrolling, revision base and one submission', async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const posts: { url: string; body: Record<string, unknown>; key: string | undefined }[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().endsWith('/messages'))
      posts.push({ url: r.url(), body: r.postDataJSON(), key: r.headers()['idempotency-key'] });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '数据对话', exact: true })).toBeVisible();
  const attachments = page.locator('.ds-chat-scroll .ds-report-attachment');
  await expect(attachments).toHaveCount(2);
  expect(await page.locator('iframe').count()).toBe(0);
  await attachments.first().getByRole('button', { name: '在对话中预览' }).click();
  await expect(attachments.first().locator('iframe')).toHaveAttribute(
    'sandbox',
    'allow-scripts allow-downloads',
  );
  await expect(attachments.first().locator('iframe')).toBeVisible();
  const report = attachments.first().frameLocator('iframe');
  await expect(
    report.getByRole('heading', { name: '月度收入分析', exact: true }).first(),
  ).toBeVisible();
  await expect(report.locator('svg').first()).toBeVisible();
  const chart = report.locator('.chart-card').first();
  await chart.getByRole('button', { name: '放大图表', exact: true }).click();
  await expect(chart.locator('[data-chart-zoom]')).not.toHaveText('100%');
  await chart.getByRole('button', { name: '重置', exact: true }).click();
  await expect(chart.locator('[data-chart-zoom]')).toHaveText('100%');
  const series = chart.locator('[data-series-toggle]').first();
  await series.click();
  await expect(series).toHaveAttribute('aria-pressed', 'false');
  await chart.getByRole('button', { name: '重置', exact: true }).click();
  await expect(series).toHaveAttribute('aria-pressed', 'true');
  const [svgDownload] = await Promise.all([
    page.waitForEvent('download'),
    chart.getByRole('button', { name: '导出 SVG', exact: true }).click(),
  ]);
  expect(svgDownload.suggestedFilename()).toMatch(/\.svg$/);
  await attachments.first().getByRole('button', { name: '收起预览' }).click();
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    attachments.first().getByRole('link', { name: '下载' }).click(),
  ]);
  expect(download.suggestedFilename()).toBe('report.html');
  const inspector = page.getByRole('complementary', { name: '当前任务检查面板' });
  await inspector.getByText('耗时与 Token', { exact: true }).click();
  await inspector.getByText('运行记录', { exact: true }).click();
  await inspector.getByText('下次提问的数据', { exact: true }).click();
  await inspector.getByText('此选择仅影响下次提交，不改变上方任务快照。').scrollIntoViewIfNeeded();
  await expect(inspector.getByText('此选择仅影响下次提交，不改变上方任务快照。')).toBeInViewport();
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <= innerWidth &&
        document.documentElement.scrollHeight <= innerHeight + 1,
    ),
  ).toBe(true);
  await page.screenshot({ path: info.outputPath('desktop.png'), fullPage: true });
  const sessionPath = new URL(page.url()).pathname.split('/')[2];
  const detail = await (await page.request.get(`/api/v1/sessions/${sessionPath}`)).json();
  const original = detail.report_versions.find((v: { number: number }) => v.number === 1)
    .artifacts[0].artifact_id;
  await attachments.first().getByRole('button', { name: '基于此版本修改' }).click();
  await expect(page.locator('.ds-revision-banner')).toContainText('版本 1');
  const input = page.getByRole('textbox', { name: '你的问题', exact: true });
  await input.fill('把标题修改为收入分析，其余保持原样');
  await input.dispatchEvent('compositionstart');
  await input.press('Enter');
  expect(posts).toHaveLength(0);
  await input.dispatchEvent('compositionend');
  await input.press('Enter');
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].body.base_report_id).toBe(original);
  expect(posts[0].key).toBeTruthy();
  await expect(page.locator('.ds-chat-scroll .ds-report-attachment')).toHaveCount(3, {
    timeout: 20000,
  });
  await page.getByRole('link', { name: '文件与报告', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: '文件与报告', level: 1, exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole('heading', { name: '文件与报告', level: 1, exact: true }),
  ).toBeVisible();
  expect(posts).toHaveLength(1);
  expect(errors).toEqual([]);
});

test('responsive layout, dark theme, accessibility and upload', async ({ page }, info) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '数据对话', exact: true })).toBeVisible();
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .analyze();
  expect(
    results.violations.map((v) => ({
      id: v.id,
      nodes: v.nodes.map((n) => ({ target: n.target, issue: n.failureSummary })),
    })),
  ).toEqual([]);
  await page.getByRole('button', { name: '切换深色主题' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.screenshot({ path: info.outputPath('desktop-dark.png'), fullPage: true });
  const dark = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  expect(
    dark.violations.map((v) => ({
      id: v.id,
      nodes: v.nodes.map((n) => ({ target: n.target, issue: n.failureSummary })),
    })),
  ).toEqual([]);
  await page.getByRole('button', { name: '切换浅色主题' }).click();
  for (const width of [1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    await expect(page.getByRole('textbox', { name: '你的问题', exact: true })).toBeInViewport();
    await page.screenshot({ path: info.outputPath(`responsive-${width}.png`), fullPage: true });
  }
  await page.getByRole('button', { name: '打开会话与数据' }).click();
  const drawer = page.getByRole('dialog', { name: '会话与数据' });
  page.on('response', (r) => {
    if (r.url().includes('/files')) {
      r.text().then((b) => console.log('FILES-DEBUG', r.status(), r.request().method(), b.slice(0, 900)));
    }
  });
  await drawer.getByLabel('选择上传文件').setInputFiles({
    name: 'sample.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('month,amount\nApr,40\nMay,50\n'),
  });
  await drawer.getByRole('button', { name: '重新读取文件' }).click();
  // The drawer tree virtualizes (off-screen leaves leave the DOM), so the
  // composer's selected-files bar is the deterministic registration signal.
  await expect(page.locator('.ds-selected-files')).toContainText('sample.csv', { timeout: 20000 });
  await drawer.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.locator('.ds-selected-files')).toContainText('sample.csv');
  await page.getByRole('button', { name: '任务与来源', exact: true }).click();
  const right = page.getByRole('dialog', { name: '任务与来源' });
  await right.getByText('下次提问的数据', { exact: true }).click();
  await expect(right.getByText('此选择仅影响下次提交，不改变上方任务快照。')).toBeVisible();
  await page.screenshot({ path: info.outputPath('mobile-inspector.png'), fullPage: true });
});

test('session isolation, file profile, lazy memory and safe session deletion', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '数据对话', level: 1 })).toBeVisible();
  const original = page.url();
  await page.getByRole('button', { name: '新建对话' }).click();
  await expect(page.getByRole('heading', { name: '从一个数据问题开始' })).toBeVisible();
  const created = page.url();
  expect(created).not.toBe(original);
  await page.getByRole('textbox', { name: '你的问题', exact: true }).fill('本会话的草稿');
  await page.getByRole('link', { name: '数据与知识库', exact: true }).click();
  const workspace = page.getByRole('region', { name: '当前工作区' });
  await workspace.getByTitle('revenue.csv', { exact: true }).click();
  const profile = page.getByRole('dialog', { name: 'revenue.csv' });
  await expect(profile).toContainText('画像状态');
  await profile.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('link', { name: 'Agent 记忆', exact: true }).click();
  await expect(page.getByText('这位专家还没有记忆记录')).toBeVisible();
  await page.getByRole('link', { name: '数据对话', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '你的问题', exact: true })).toHaveValue(
    '本会话的草稿',
  );
  await page.getByRole('button', { name: '删除当前会话', exact: true }).click();
  await page
    .getByRole('dialog', { name: '删除当前会话？' })
    .getByRole('button', { name: '删除会话', exact: true })
    .click();
  await expect(page).toHaveURL(original);
  await expect(page.locator('.ds-chat-scroll .ds-report-attachment')).toHaveCount(3);
});

test('polling fallback completes a task without duplicated POST; cancellation remains available', async ({
  page,
}) => {
  await page.route('**/stream?*', (r) => r.abort());
  let submissions = 0;
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().endsWith('/messages')) submissions++;
  });
  await page.goto('/');
  await expect(page.getByRole('textbox', { name: '你的问题', exact: true })).toBeVisible();
  const input = page.getByRole('textbox', { name: '你的问题', exact: true });
  await input.fill('查询平均收入');
  await input.press('Enter');
  await expect(page.locator('.ds-chat-scroll')).toContainText('平均收入为 20。', {
    timeout: 20000,
  });
  expect(submissions).toBe(1);
  await input.fill('取消测试：分析收入');
  await input.press('Enter');
  await page
    .getByRole('complementary', { name: '当前任务检查面板' })
    .getByRole('button', { name: '取消任务', exact: true })
    .click();
  await expect(page.getByRole('complementary', { name: '当前任务检查面板' })).toContainText(
    '已取消',
    { timeout: 20000 },
  );
  expect(submissions).toBe(2);
});

test('clarification restores its task context and honors an explicitly cleared report format', async ({
  page,
}) => {
  const posts: Record<string, unknown>[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().endsWith('/messages')) posts.push(r.postDataJSON());
  });
  await page.goto('/');
  await page.getByRole('button', { name: '新建对话' }).click();
  await expect(page.getByRole('heading', { name: '从一个数据问题开始' })).toBeVisible();
  await page.getByRole('checkbox', { name: /revenue\.csv/ }).click();
  const format = page.getByRole('combobox', { name: '报告输出格式' });
  await format.click();
  await expect(page.getByText('HTML 报告', { exact: true })).toBeInViewport({ timeout: 5000 });
  // Ant Design's virtual list exposes a hidden ARIA mirror; click its visible label.
  await page.getByText('HTML 报告', { exact: true }).click();
  await format.press('Escape');
  const input = page.getByRole('textbox', { name: '你的问题', exact: true });
  await input.fill('需要补充示例');
  await input.press('Enter');
  await expect(page.locator('.ds-clarification')).toBeVisible();
  await page.locator('.ds-clarification').getByRole('button', { name: '补充说明' }).click();
  await expect(page.locator('.ds-composer-context')).toContainText('HTML 报告');
  await format.focus();
  await format.press('Backspace');
  await format.press('Escape');
  await expect(page.locator('.ds-composer-context')).not.toContainText('HTML 报告');
  await input.fill('一月至三月，直接给出平均收入');
  await input.press('Enter');
  await expect(page.locator('.ds-chat-scroll')).toContainText('平均收入为 20。', {
    timeout: 20000,
  });
  expect(posts).toHaveLength(2);
  expect(posts[1].reply_to_task_id).toBeTruthy();
  expect(posts[1].expected_outputs).toEqual([]);
  await expect(page.locator('.ds-chat-scroll .ds-report-attachment')).toHaveCount(0);
});

test('format menu stays visible with normal motion on a narrow screen', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto('/');
  const format = page.getByRole('combobox', { name: '报告输出格式' });
  await format.click();
  await expect(page.getByText('HTML 报告', { exact: true })).toBeInViewport();
  await page.getByText('HTML 报告', { exact: true }).click();
  await format.press('Escape');
  await expect(page.locator('.ds-composer-context')).toContainText('HTML 报告');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('sidebar conversation management: rename, clear and per-item delete', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '数据对话', level: 1 })).toBeVisible();
  // The seeded 长会话性能验收 session is never the active one here.
  const seedTitle = '长会话性能验收';
  const actionsFor = (title: string) => page.getByRole('button', { name: `会话操作：${title}` });
  await page.getByRole('link', { name: seedTitle }).hover();
  await actionsFor(seedTitle).click();
  await page.getByRole('menuitem', { name: '重命名' }).click();
  const renameDialog = page.getByRole('dialog', { name: '重命名会话' });
  await renameDialog.getByRole('textbox', { name: '会话名称' }).fill('改名的长会话');
  await renameDialog.getByRole('button', { name: /保\s*存/ }).click();
  await expect(actionsFor('改名的长会话')).toBeVisible();

  await page.getByRole('link', { name: '改名的长会话' }).hover();
  await actionsFor('改名的长会话').click();
  await page.getByRole('menuitem', { name: '清空记录' }).click();
  await page
    .getByRole('dialog', { name: '清空对话记录？' })
    .getByRole('button', { name: '清空记录', exact: true })
    .click();
  await page.getByRole('link', { name: '改名的长会话' }).click();
  await expect(page.getByRole('heading', { name: '从一个数据问题开始' })).toBeVisible();

  await page.getByRole('link', { name: '改名的长会话' }).hover();
  await actionsFor('改名的长会话').click();
  await page.getByRole('menuitem', { name: '删除对话' }).click();
  await page
    .getByRole('dialog', { name: '删除对话？' })
    .getByRole('button', { name: '删除对话', exact: true })
    .click();
  await expect(actionsFor('改名的长会话')).toHaveCount(0);
});

test('first question names the session in the sidebar', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '新建对话' }).click();
  await expect(page.getByRole('heading', { name: '从一个数据问题开始' })).toBeVisible();
  const input = page.getByRole('textbox', { name: '你的问题', exact: true });
  await input.fill('查询平均收入是多少');
  await input.press('Enter');
  await expect(page.locator('.ds-chat-scroll')).toContainText('平均收入为 20。', { timeout: 20000 });
  await expect(page.getByRole('link', { name: '查询平均收入是多少' })).toBeVisible();
});

test('running task shows the phase and elapsed time in the inspector', async ({ page }) => {
  await page.route('**/stream?*', (r) => r.abort());
  await page.goto('/');
  const input = page.getByRole('textbox', { name: '你的问题', exact: true });
  await input.fill('取消测试：分析收入');
  await page.getByRole('button', { name: '发送问题' }).click();
  // The submitted bubble is the durable signal that the task was accepted.
  await expect(
    page.locator('.ds-chat-scroll').getByText('取消测试：分析收入', { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  const inspector = page.locator('aside[aria-label="当前任务检查面板"]');
  await expect(inspector.getByText(/已进行/)).toBeVisible({ timeout: 30000 });
  await expect(inspector.getByText(/^(排队中|正在启动)$/).first()).toBeVisible({ timeout: 30000 });
  await inspector.getByRole('button', { name: '取消任务' }).click();
  await expect(inspector.getByText('已取消')).toBeVisible({ timeout: 15000 });
});
