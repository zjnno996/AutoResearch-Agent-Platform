import { test, expect } from '@playwright/test';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');

for (const missing of [false, true]) {
  test(`PDF print ${missing ? 'reports a missing figure' : 'embeds project attachment images before printing'}`, async ({ page, context }) => {
    await context.addInitScript(() => {
      (window as any).__printCalls = 0;
      window.print = () => { (window as any).__printCalls += 1; };
    });
    await page.route('**/download/pdf-fixture/experiment%2Foutputs%2Ffit.png', route => route.fulfill(missing ? {
      status: 404, body: 'Missing figure',
    } : {
      status: 200, contentType: 'application/octet-stream',
      headers: { 'Content-Disposition': 'attachment; filename="fit.png"' }, body: png,
    }));
    await page.goto('/');
    await page.evaluate(async () => {
      const module = await import('/src/export.ts');
      const button = document.createElement('button');
      button.textContent = 'Print fixture';
      button.onclick = () => module.printContentAsPdf('# Reproducible report\n\n![Actual plot](../stage-11/experiment/outputs/fit.png)', 'PDF export', {
        resolveImageUrl: () => location.origin + '/download/pdf-fixture/experiment%2Foutputs%2Ffit.png',
      });
      document.body.append(button);
    });
    const popupReady = page.waitForEvent('popup');
    await page.getByRole('button', { name: 'Print fixture' }).click();
    const popup = await popupReady;
    await expect(popup.locator('html')).toHaveAttribute('data-print-state', missing ? 'error' : 'ready', { timeout: 8000 });
    if (missing) {
      await expect(popup.getByRole('alert')).toContainText('图表加载失败');
      expect(await popup.evaluate(() => (window as any).__printCalls)).toBe(0);
    } else {
      expect(await popup.locator('img').evaluate(image => ({ loaded: (image as HTMLImageElement).naturalWidth, source: (image as HTMLImageElement).src.startsWith('data:image/png') }))).toEqual({ loaded: 1, source: true });
      expect(await popup.evaluate(() => (window as any).__printCalls)).toBe(1);
      expect((await popup.pdf()).length).toBeGreaterThan(1000);
    }
    await popup.close();
  });
}
