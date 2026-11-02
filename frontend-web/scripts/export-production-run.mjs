import { chromium, expect } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const artifacts = join(root, '.local/production/verification');
const run = JSON.parse(await readFile(join(artifacts, 'research-run.json'), 'utf8'));
const state = JSON.parse(await readFile(join(root, 'backend/runs/production/projects', run.id, 'compact_state.json'), 'utf8'));
if (!['ground','plan','implement','execute','report','verify'].every(name => state.phases[name]?.status === 'done')) {
  throw new Error('The preselected research run has not completed all six phases.');
}
const access = JSON.parse(await readFile(process.env.EMNETS_PRODUCTION_ACCESS_FILE ||
  join(process.env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'emnets-ra/production-access.json'), 'utf8'));
const base = process.env.EMNETS_PUBLIC_URL || run.base_url;
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 },
  httpCredentials: { username: access.username, password: access.password, origin: base } });
// Automate the browser's real print document; no OS print dialog in headless QA.
await context.addInitScript(() => { window.print = () => {}; });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto(base, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.locator('#auth-username').fill(access.username);
  await page.locator('#auth-password').fill(access.password);
  await page.getByRole('button', { name: '登录工作空间', exact: true }).click();
  await expect(page.getByRole('heading', { name: /^实验室总览/ })).toBeVisible({ timeout: 30000 });
  await page.goto(base+'/#/research-lab?project='+encodeURIComponent(run.id));
  await expect(page.locator('.research-pipeline-progress')).toContainText('6/6 阶段', { timeout: 30000 });
  const final = page.locator('section.artifact-section').filter({ has: page.getByRole('heading', { name: '最终产物', exact: true }) });
  const downloads = [];
  for (const [label, filename] of [['paper.md','research-report.md'],['实验代码与结果 ZIP','experiment.zip'],['数据与图表 ZIP','outputs.zip']]) {
    const ready = page.waitForEvent('download', { timeout: 60000 });
    await final.getByRole('link', { name: label, exact: true }).click();
    const download = await ready;
    await download.saveAs(join(artifacts, filename));
    downloads.push(filename);
  }
  const pdfReady = page.waitForEvent('popup', { timeout: 30000 });
  await final.getByRole('button', { name: '📄 PDF', exact: true }).click();
  const printPage = await pdfReady;
  await printPage.waitForFunction(() => document.documentElement.dataset.printState === 'ready'
    || document.documentElement.dataset.printState === 'error', null, { timeout: 120000 });
  expect(await printPage.locator('html').getAttribute('data-print-state')).toBe('ready');
  const embeddedImages = await printPage.locator('img').evaluateAll(images => images.map(image => ({
    embedded: image.src.startsWith('data:image/'), width: image.naturalWidth, height: image.naturalHeight,
  })));
  expect(embeddedImages.length).toBeGreaterThanOrEqual(2);
  expect(embeddedImages.every(image => image.embedded && image.width > 0 && image.height > 0)).toBe(true);
  await printPage.pdf({ path: join(artifacts, 'research-report.pdf'), format: 'A4',
    printBackground: true, preferCSSPageSize: true });
  await printPage.close();
  downloads.push('research-report.pdf');
  await page.screenshot({ path: join(artifacts, 'production-research-completed.png'), fullPage: true });
  await page.reload();
  await expect(page.locator('.research-pipeline-progress')).toContainText('6/6 阶段', { timeout: 30000 });
  expect(errors).toEqual([]);
  const result = { base_url: base, id: run.id, downloads, pdf_export: 'browser_print_popup',
    embedded_images: embeddedImages, browser_errors: errors, persisted_after_reload: true };
  await writeFile(join(artifacts, 'downloads.json'), JSON.stringify(result, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
