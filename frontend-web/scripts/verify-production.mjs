import { chromium, expect } from '@playwright/test';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const base = process.env.EMNETS_PUBLIC_URL;
if (!base) throw new Error('Set EMNETS_PUBLIC_URL to the deployed HTTPS address.');
const access = JSON.parse(await readFile(process.env.EMNETS_PRODUCTION_ACCESS_FILE ||
  join(process.env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'emnets-ra/production-access.json'), 'utf8'));
const artifacts = join(root, '.local/production/verification');
await mkdir(artifacts, { recursive: true, mode: 0o700 });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
  httpCredentials: { username: access.username, password: access.password, origin: base } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  let loaded = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    const response = await page.goto(base, { waitUntil: 'domcontentloaded', timeout: 60000 });
    if (response?.ok()) {
      await page.locator('#auth-username').waitFor({ state: 'visible', timeout: 30000 });
      loaded = true; break;
    }
    if (!response || response.status() < 500) throw new Error('Unexpected public entry response: '+response?.status());
    console.log('Public tunnel is reconnecting; retrying page load without creating a task.');
    await page.waitForTimeout(5000);
  }
  if (!loaded) throw new Error('The public tunnel has not recovered; no research task was submitted.');
  console.log('Public HTTPS page loaded');
  await page.locator('#auth-username').fill(access.username);
  await page.locator('#auth-password').fill(access.password);
  await page.getByRole('button', { name: '登录工作空间', exact: true }).click();
  await expect(page.getByRole('heading', { name: /^实验室总览/ })).toBeVisible({ timeout: 30000 });
  console.log('Application login and public WebSocket verified');
  await expect(page.locator('.metric-card').filter({ hasText: '可用研究技能' }).locator('strong')).toHaveText('210');
  await page.getByRole('link', { name: '研究技能库', exact: true }).click();
  await expect(page.getByRole('heading', { name: /^研究技能库/ })).toBeVisible();
  await page.getByLabel('研究问题', { exact: true }).fill('least-squares with an independent holdout');
  await page.getByRole('button', { name: '预览技能匹配' }).click();
  await expect(page.locator('.matched-skills')).toBeVisible();
  await page.getByRole('link', { name: '工作空间设置', exact: true }).click();
  await expect(page.getByText('Qwen3.8-27B-FP8', { exact: true })).toBeVisible();
  await expect(page.locator('.service-list').filter({ hasText: '学术评审服务' })).toContainText('已连接');
  await page.screenshot({ path: join(artifacts, 'production-settings.png'), fullPage: true });
  await page.getByRole('link', { name: '实验室总览', exact: true }).click();
  await page.screenshot({ path: join(artifacts, 'production-overview.png'), fullPage: true });
  const result = { base_url: base, browser_errors: errors, real_login: true,
    skill_routing: true, review_service_connected: true, checked_at: new Date().toISOString() };
  if (process.argv.includes('--submit')) {
    const record = join(artifacts, 'research-run.json');
    try {
      const existing = JSON.parse(await readFile(record, 'utf8'));
      throw new Error('A preselected production run already exists: '+existing.id+'; inspect it instead of resubmitting.');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await page.getByRole('link', { name: /开始一项研究/ }).click();
    await page.getByRole('button', { name: '完整研究流程', exact: true }).click();
    const attempt = process.env.EMNETS_ACCEPTANCE_ATTEMPT || '1';
    if (!/^[1-9][0-9]*$/.test(attempt)) throw new Error('Invalid explicit acceptance attempt number.');
    const marker = attempt === '1' ? '[PROD-E2E-20261102]' : `[PROD-E2E-20261102-R${attempt}]`;
    const topic = marker+' 完成一个固定、可复核的数值实验，只使用 Python 标准库、NumPy、Pillow，不下载数据、不检索外部资料、不使用 GPU、不扩展研究方向。训练集 x=[-3,-2,-1,0,1], y=[-11,-8,-5,-2,1]，独立测试集 x=[2,3,4], y=[4,7,10]。仅在训练集使用 numpy.linalg.lstsq 拟合 y=a*x+b；禁止用测试集拟合、选参或修改数据。基线始终预测训练集 y 的均值。真实执行代码，将平铺数值键 a,b,train_mse,test_mse,test_r2,baseline_test_mse 写入 outputs/metrics.json。输出 outputs/predictions.csv，包含全部8行原始点及 split、x、y、prediction、baseline_prediction。用 Pillow 绘制真实散点与拟合线 PNG，以及测试集模型与基线误差比较 PNG，写清坐标、图例和单位。输出可独立重跑的代码与简洁研究报告，引用真实生成的图。独立复算参数、预测、残差、基线和数据行数；说明这是无噪声的有限玩具实验，不支持泛化结论，不虚构文献或外部实验。';
    await page.locator('#research-topic').fill(topic);
    await page.getByRole('button', { name: '创建并开始研究', exact: true }).click();
    const project = page.locator('.project-item').filter({ hasText: marker });
    await expect(project).toBeVisible({ timeout: 30000 });
    const id = await project.locator('strong').innerText();
    result.research_run = { id, topic, base_url: base, submitted_at: new Date().toISOString() };
    await writeFile(record, JSON.stringify(result.research_run, null, 2), { mode: 0o600 });
    await page.screenshot({ path: join(artifacts, 'production-research-submitted.png'), fullPage: true });
  }
  expect(errors).toEqual([]);
  await writeFile(join(artifacts, 'browser-checks.json'), JSON.stringify(result, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(result));
} catch (error) {
  await page.screenshot({ path: join(artifacts, 'production-check-failed.png'), fullPage: true }).catch(() => {});
  throw error;
} finally { await browser.close(); }
