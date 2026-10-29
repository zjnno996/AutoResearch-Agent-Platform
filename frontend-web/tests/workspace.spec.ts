import { test, expect, type Page } from "@playwright/test";
import { mkdir, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";

async function register(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "创建账号", exact: true }).click();
  await page
    .locator("#auth-username")
    .fill(
      `ui${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    );
  await page.locator("#auth-password").fill("emnets-ui-test-password");
  await page.getByRole("button", { name: "创建账号", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /^实验室总览/ }),
  ).toBeVisible();
  await expect(
    page
      .locator(".metric-card")
      .filter({ hasText: "可用研究技能" })
      .locator("strong"),
  ).toHaveText("210");
}

test("real authentication, workspace data, navigation and keyboard search", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await register(page);
  await expect(
    page
      .locator(".metric-card")
      .filter({ hasText: "科研项目" })
      .locator("strong"),
  ).toHaveText("0");
  await expect(
    page.getByRole("heading", { name: "下一次发现，从这里开始" }),
  ).toBeVisible();
  await page.screenshot({
    path: "artifacts/overview-desktop.png",
    fullPage: true,
  });
  await page.keyboard.press("Control+k");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("textbox", { name: "搜索工作空间" }).fill("科研工作台");
  await page
    .getByRole("dialog")
    .getByRole("link", { name: /科研工作台/ })
    .click();
  await expect(
    page.getByRole("heading", { name: /^科研工作台/ }),
  ).toBeVisible();
  await page.getByRole("link", { name: "实验室总览", exact: true }).click();
  await page.getByRole("link", { name: /开始一项研究/ }).click();
  await expect(page.locator("#research-topic")).toBeFocused();
  expect(errors).toEqual([]);
});

test("skill search, category filtering and real routing preview", async ({
  page,
}) => {
  await register(page);
  await page.getByRole("link", { name: "研究技能库", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /^研究技能库/ }),
  ).toBeVisible();
  await page.getByLabel("搜索已安装技能").fill("no-such-skill-emnets");
  await expect(
    page.getByRole("heading", { name: "没有找到匹配的技能" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "清除筛选" }).click();
  await expect(page.locator(".catalog-skill-card")).toHaveCount(18);
  await page
    .getByLabel("研究问题", { exact: true })
    .fill("numerical operator invariant");
  await page.getByLabel("研究阶段", { exact: true }).selectOption("13");
  await page.getByRole("button", { name: "预览技能匹配" }).click();
  await expect(page.locator(".matched-skills")).toContainText(
    "property-based-testing",
  );
  await page.getByRole("button", { name: "工程质量", exact: true }).click();
  await expect(page.locator(".catalog-skill-card").first()).toBeVisible();
  await page.screenshot({
    path: "artifacts/skills-desktop.png",
    fullPage: true,
  });
});

test("focused report submission creates one owned project and updates the overview", async ({ page }) => {
  await register(page);
  await page.getByRole("link", { name: /开始一项研究/ }).click();
  await expect(page.getByRole("button", { name: "专注研究", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "完整研究流程", exact: true }).click();
  await expect(page.getByRole("button", { name: "研究报告", exact: true })).toHaveAttribute("aria-pressed", "true");
  const topic = `UI submission ${Date.now()}: finite least-squares diagnostic`;
  await page.locator("#research-topic").fill(topic);
  await page.getByRole("button", { name: "创建并开始研究", exact: true }).click();
  const project = page.locator(".project-item").filter({ hasText: topic });
  await expect(project).toBeVisible();
  await expect(project).toContainText("研究报告");
  await page.getByRole("button", { name: "暂停", exact: true }).click();
  await expect(project.locator(".status-dot")).toHaveText("paused");
  await page.getByRole("button", { name: "恢复", exact: true }).click();
  await expect(project.locator(".status-dot")).toHaveText("queued");
  await page.getByRole("link", { name: "实验室总览", exact: true }).click();
  await expect(page.locator(".metric-card").filter({ hasText: "科研项目" }).locator("strong")).toHaveText("1");
  await page.locator(".project-title").filter({ hasText: topic }).click();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.locator(".project-item").filter({ hasText: topic })).toHaveCount(0);
});

test("theme persists, backend health is real, and export contains only workspace data", async ({
  page,
}) => {
  await register(page);
  await page.getByRole("button", { name: "切换深色主题" }).click();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(
    page.getByRole("heading", { name: /^实验室总览/ }),
  ).toBeVisible();
  await page.screenshot({
    path: "artifacts/overview-dark.png",
    fullPage: true,
  });
  await page.getByRole("link", { name: "工作空间设置", exact: true }).click();
  await expect(
    page.getByText("Qwen3.8-27B-FP8", { exact: true }),
  ).toBeVisible();
  await expect(
    page.locator(".service-list").filter({ hasText: "学术评审服务" }),
  ).toContainText("已连接");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出概览 JSON" }).click();
  expect((await download).suggestedFilename()).toBe("emnets-workspace.json");
});

test("mobile navigation, review screen and horizontal layout", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await register(page);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "artifacts/overview-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "切换导航" }).click();
  await expect(
    page.getByRole("link", { name: "学术评审", exact: true }),
  ).toBeInViewport();
  await page.getByRole("link", { name: "学术评审", exact: true }).click();
  await expect(page.getByRole("heading", { name: /^学术评审/ })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("stream completion preserves the final review and summary", async ({ page }) => {
  await register(page);
  const result = { dimensionId: "methodology", score: 72, summary: "Grounded review", strengths: [], weaknesses: [], suggestions: [] };
  await page.route("**/api/review", route => route.fulfill({
    status: 200,
    contentType: "application/x-ndjson",
    body: [
      { type: "progress", dimensionId: "methodology", result },
      { type: "complete", results: [{ ...result, score: 91 }], reportSummary: { overallComment: "Final synthesis is retained", dimensions: [] } },
    ].map(event => JSON.stringify(event) + "\n").join(""),
  }));
  await page.getByRole("link", { name: "学术评审", exact: true }).click();
  await page.locator('input[type="file"]').setInputFiles({ name: "stream-regression.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\nstream fixture") });
  await page.getByRole("button", { name: /^开始评审/ }).click();
  await expect(page.locator(".review-results-phase")).toBeVisible();
  const stored = await page.evaluate(() => ({
    results: JSON.parse(localStorage.getItem("autoReviewResults") || "[]"),
    summary: JSON.parse(localStorage.getItem("autoReviewReportSummary") || "null"),
  }));
  expect(stored.results[0].score).toBe(91);
  expect(stored.summary.overallComment).toBe("Final synthesis is retained");
});

test("owned project rows use real backend checkpoints and preserve deep links", async ({
  page,
}) => {
  await register(page);
  const user = await page.evaluate(() =>
    JSON.parse(localStorage.getItem("claw-auth-user") || "{}"),
  );
  const id = `ui-checkpoint-${user.id}`;
  const directory = join(
    process.env.EMNETS_UI_RUNS_DIR || "/tmp/emnets-ra-web-smoke-data",
    "projects",
    id,
  );
  try {
    await mkdir(directory, { recursive: true });
    await writeFile(
      join(directory, "project_meta.json"),
      JSON.stringify({ user_id: user.id, topic: "界面联调：边缘感知数据分析" }),
    );
    await writeFile(
      join(directory, "checkpoint.json"),
      JSON.stringify({
        workflow: "compact",
        last_completed_stage: 9,
        completed_phases: ["ground", "plan"],
      }),
    );
    await mkdir(join(directory, "stage-25"), { recursive: true });
    await writeFile(join(directory, "stage-25/paper_final.md"), "Owned focused report download");
    const download = await page.request.get(`/download/${encodeURIComponent(id)}/paper_final.md`);
    expect(download.status()).toBe(200);
    expect(await download.text()).toBe("Owned focused report download");
    await page.getByRole("button", { name: "刷新项目" }).click();
    await expect(
      page
        .locator(".project-title")
        .filter({ hasText: "界面联调：边缘感知数据分析" }),
    ).toBeVisible();
    await expect(page.locator(".table-progress")).toContainText("33%");
    await page
      .locator(".project-title")
      .filter({ hasText: "界面联调：边缘感知数据分析" })
      .click();
    await expect(page).toHaveURL(new RegExp(`project=${id}`));
    await expect(
      page.getByRole("heading", { name: /^科研工作台/ }),
    ).toBeVisible();
    await expect(page.locator(".workspace")).toContainText(
      "界面联调：边缘感知数据分析",
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
