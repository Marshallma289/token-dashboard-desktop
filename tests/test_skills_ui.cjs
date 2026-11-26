// Run with NODE_PATH pointing to Playwright, or install it in the test environment.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const { once } = require('node:events');

async function freePort() {
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const { port } = socket.address();
  await new Promise(resolve => socket.close(resolve));
  return port;
}

async function main() {
  const temp = await fs.realpath(os.tmpdir());
  const root = await fs.mkdtemp(path.join(temp, 'dashboard-skills-ui-'));
  let browser;
  let server;
  try {
    const names = { codex: 'codex-external-fixture', hermes: 'hermes-external-fixture' };
    for (const agent of Object.keys(names)) {
      const location = agent === 'codex' ? path.join(root, agent, 'skills', '.system', names[agent])
        : path.join(root, agent, 'skills', names[agent]);
      await fs.mkdir(location, { recursive: true });
      await fs.writeFile(path.join(location, 'SKILL.md'), `---\nname: ${names[agent]}\ndescription: external fixture\n---\n<script>throw new Error('not rendered as HTML')</script>\n`);
    }
    const input = path.join(root, 'independent-input');
    for (const name of ['catalog-alpha', 'catalog-beta']) {
      const location = path.join(input, name);
      await fs.mkdir(location, { recursive: true });
      await fs.writeFile(path.join(location, 'SKILL.md'), `---\nname: ${name}\ndescription: Browser regression fixture\n---\n`);
    }
    const logs = path.join(root, 'empty-logs');
    await fs.mkdir(logs);
    const port = await freePort();
    const url = `http://127.0.0.1:${port}`;
    server = spawn(process.env.PYTHON || 'python', ['-B', 'backend.py', 'serve', '--db', path.join(root, 'usage.sqlite'), '--root', logs, '--hermes-db', path.join(root, 'no-hermes.sqlite'), '--skills-data-dir', path.join(root, 'library'), '--port', String(port), '--no-open'], {
      cwd: path.resolve(__dirname, '..'),
      env: { ...process.env, CODEX_HOME: path.join(root, 'codex'), HERMES_HOME: path.join(root, 'hermes'), DEEPL_AUTH_KEY: 'fixture:fx', PYTHONUTF8: '1' }, stdio: ['ignore', 'pipe', 'pipe']
    });
    let serverLog = '';
    server.stdout.on('data', value => { serverLog += value; });
    server.stderr.on('data', value => { serverLog += value; });
    for (let tries = 0; ; tries++) {
      const health = await fetch(`${url}/api/health`).catch(() => null);
      if (health?.ok) break;
      if (tries >= 100 || server.exitCode !== null) throw new Error(`Dashboard did not start:\n${serverLog}`);
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ ...(process.env.SKILLS_EDGE ? { executablePath: process.env.SKILLS_EDGE } : {}), args: ['--no-first-run'] });
    const page = await browser.newPage({ viewport: { width: 1280, height: 760 }, acceptDownloads: true });
    page.setDefaultTimeout(6000);
    const errors = [];
    const calls = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.url() === `${url}/api/skills`) calls.push(request.postDataJSON());
    });
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    const tab = page.getByRole('tab', { name: '技能管理' });
    await tab.focus();
    await page.keyboard.press('Enter');
    const ws = page.locator('#view-skills');
    const nav = ws.getByRole('navigation', { name: '技能管理页面' });
    const dialog = ws.getByRole('dialog');
    const section = view => nav.getByRole('button', { name: new RegExp(`^${view}`) }).click();
    const wait = async (locator) => locator.waitFor({ state: 'visible' });
    await wait(ws.getByRole('heading', { name: 'Codex 全局工作区' }));
    assert.equal(await page.locator('#main > .footer').isVisible(), false);
    await section('已装');
    const codex = ws.getByRole('button', { name: `查看 ${names.codex} 详情` });
    await wait(codex);
    assert.equal(await ws.locator('.skills-card-meta').textContent(), '外部');
    await codex.click();
    await wait(dialog.getByText("<script>throw new Error('not rendered as HTML')</script>", { exact: false }));
    assert.equal(await dialog.locator('script').count(), 0);
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    await ws.getByRole('button', { name: 'Hermes', exact: true }).click();
    await section('外部');
    await wait(ws.getByRole('button', { name: `查看 ${names.hermes} 详情` }));
    assert.equal(await codex.count(), 0);
    await ws.getByRole('button', { name: 'Codex', exact: true }).click();
    await section('技能库');

    await ws.locator('.skills-context').getByRole('button', { name: '导入技能' }).click();
    assert.equal(await dialog.getByRole('button', { name: '选择文件夹' }).count(), 0);
    await dialog.getByLabel('路径', { exact: true }).fill(input);
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });

    // The browser keeps its manual-path fallback; a desktop bridge fills the same form.
    const archivePath = path.join(root, 'chosen.skill');
    await page.evaluate(({ input, archivePath }) => {
      window.pywebview = { api: {
        choose_skills_directory: async agent => ({ ok: agent === 'codex', path: input }),
        choose_skills_archive: async agent => ({ ok: agent === 'codex', path: archivePath })
      } };
    }, { input, archivePath });
    await ws.locator('.skills-context').getByRole('button', { name: '导入技能' }).click();
    await dialog.getByRole('button', { name: '压缩包', exact: true }).click();
    await dialog.getByRole('button', { name: '选择文件', exact: true }).click();
    await page.waitForFunction(archivePath => document.querySelector('dialog[open] input[name=path]')?.value === archivePath, archivePath);
    await dialog.getByRole('button', { name: '本地目录', exact: true }).click();
    await dialog.getByRole('button', { name: '选择文件夹', exact: true }).click();
    await page.waitForFunction(input => document.querySelector('dialog[open] input[name=path]')?.value === input, input);
    assert.equal(calls.filter(call => /_preview$/.test(call.op)).length, 0);
    await dialog.getByRole('button', { name: '预览技能' }).click();
    await dialog.getByRole('button', { name: '全选', exact: true }).click();
    await dialog.getByRole('button', { name: '导入所选技能' }).click();
    await dialog.waitFor({ state: 'hidden' });
    await wait(ws.locator('.skills-notice').getByText('已导入 2 项，失败 0 项。已加入技能库，尚未安装。'));
    assert.equal(calls.filter(call => call.op === 'install').length, 0);
    await ws.getByRole('button', { name: '查看本次导入' }).click();
    await ws.getByRole('button', { name: '批量安装' }).click();
    await dialog.getByRole('button', { name: '确认安装' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(calls.find(call => call.op === 'install').payload.ids, ['catalog-alpha', 'catalog-beta']);
    await ws.getByLabel('搜索技能').fill('alpha');
    await ws.getByRole('checkbox', { name: '选择 catalog-alpha' }).check();
    await ws.getByRole('button', { name: '批量停用' }).click();
    await dialog.getByRole('button', { name: '确认停用' }).click();
    await dialog.waitFor({ state: 'hidden' });
    await ws.getByRole('checkbox', { name: '选择 catalog-alpha' }).check();
    await ws.getByRole('button', { name: '批量删除' }).click();
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(calls.filter(call => call.op === 'delete').length, 0);
    await ws.getByRole('button', { name: '批量删除' }).click();
    await dialog.getByRole('button', { name: '确认删除' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(calls.find(call => call.op === 'delete').payload.ids, ['catalog-alpha']);
    await ws.getByLabel('搜索技能').fill('');

    if (process.env.SKILLS_QA_DIR) await page.screenshot({ path: path.join(process.env.SKILLS_QA_DIR, 'skills-desktop.png') });
    await page.setViewportSize({ width: 375, height: 722 });
    await page.evaluate(() => { document.body.dataset.theme = 'dark'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.ok((await ws.getByRole('checkbox', { name: '选择 catalog-beta' }).locator('..').boundingBox()).width >= 44);
    if (process.env.SKILLS_QA_DIR) await page.screenshot({ path: path.join(process.env.SKILLS_QA_DIR, 'skills-narrow.png'), fullPage: true });

    await section('已装');
    const discovered = ws.locator('.skills-card').filter({
      has: page.getByRole('button', { name: `查看 ${names.codex} 详情` })
    });
    await discovered.getByRole('button', { name: '导入库' }).click();
    await dialog.getByRole('button', { name: '预览技能' }).click();
    await dialog.getByRole('button', { name: '全选', exact: true }).click();
    await dialog.getByRole('button', { name: '导入所选技能' }).click();
    await dialog.waitFor({ state: 'hidden' });
    await wait(discovered.getByText('已装 · 受保护'));
    assert.equal(await discovered.count(), 1);
    await section('技能库');
    await wait(discovered.getByText('已装 · 受保护'));
    assert.equal(await discovered.getByRole('button', { name: '安装', exact: true }).count(), 0);
    assert.equal(await discovered.getByRole('button', { name: '停用', exact: true }).count(), 0);
    await codex.click();
    await wait(dialog.getByText(path.join(root, 'codex', 'skills', '.system', names.codex), { exact: true }));
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });

    await section('设置');
    await ws.getByRole('button', { name: '查看日志' }).click();
    await wait(dialog.getByText('安装', { exact: true }));
    const download = page.waitForEvent('download');
    await dialog.getByRole('button', { name: '导出日志' }).click();
    assert.equal((await download).suggestedFilename(), 'skills-activity-codex.jsonl');
    await page.keyboard.press('Escape');

    const sync = { connected: true, git_available: true, remote_url: 'git@github.com:test/private.git', state: 'local_changes' };
    const conflict = { remote_revision: 'a'.repeat(40), local_digest: 'b'.repeat(64), choices: ['keep_local', 'use_remote', 'keep_both'], skill_ids: ['catalog-beta'] };
    const fresh = { ...conflict, remote_revision: 'c'.repeat(40) };
    const resolve = [];
    await page.route('**/api/skills', async route => {
      const { op, payload } = route.request().postDataJSON();
      const ok = result => route.fulfill({ json: { ok: true, result } });
      const fail = (code, current) => route.fulfill({ status: 409, json: { ok: false, error: code, code, conflict: current } });
      if (op === 'sync_status') return ok({ status: sync });
      if (op === 'sync_push') return fail('sync_conflict', conflict);
      if (op === 'sync_resolve') {
        resolve.push(payload);
        return resolve.length === 1 ? fail('sync_conflict_stale', fresh) : ok({ status: { ...sync, state: 'in_sync' }, changed: true, merge: { mode: 'keep_both', published: true, renamed_skills: [{ from_id: 'catalog-beta', to_id: 'catalog-beta-remote' }] } });
      }
      if (op === 'github_status') return ok({ available: true, authenticated: true, login: 'fixture-user' });
      if (op === 'github_connect') return ok({ remote: 'https://github.com/fixture-user/private.git', created: true, credential_helper: 'gh' });
      if (op === 'sync_connect') return ok({ status: { ...sync, remote_url: payload.remote_url } });
      return route.continue();
    });
    await section('备份');
    await ws.getByRole('button', { name: '推送到远端' }).click();
    await dialog.getByRole('button', { name: '确认推送' }).click();
    await wait(dialog.getByRole('radio', { name: /保留本机/ }));
    await wait(dialog.getByRole('radio', { name: /使用远端/ }));
    for (const snapshot of [conflict, fresh]) {
      await dialog.getByRole('radio', { name: /保留两份/ }).check();
      await dialog.getByRole('button', { name: '下一步' }).click();
      assert.equal(resolve.length, snapshot === conflict ? 0 : 1);
      await dialog.getByRole('button', { name: '确认', exact: true }).click();
      await page.waitForFunction(expected => document.querySelector('.skills-dialog h2')?.textContent.includes(expected), snapshot === conflict ? '处理同步冲突' : '合并结果');
      assert.deepEqual(resolve.at(-1), { strategy: 'keep_both', expected_remote_revision: snapshot.remote_revision, expected_local_digest: snapshot.local_digest, confirm: true });
    }
    await wait(dialog.getByText('新名称：catalog-beta-remote'));
    await page.keyboard.press('Escape');
    await ws.getByRole('button', { name: '通过 GitHub 连接' }).click();
    await dialog.getByRole('button', { name: '新建私有仓库' }).click();
    await dialog.getByRole('button', { name: '核对连接' }).click();
    assert.equal(calls.filter(call => call.op === 'github_connect').length, 0);
    await dialog.getByRole('button', { name: '确认创建并连接' }).click();
    await dialog.waitFor({ state: 'hidden' });
    const finalConnect = calls.find(call => call.op === 'sync_connect');
    assert.equal(finalConnect.payload.credential_helper, 'gh');
    assert.equal(calls.filter(call => call.op === 'sync_push').length, 1);

    const auto = { codex: { enabled: false, state: 'disabled', paused: false }, hermes: { enabled: false, state: 'disabled', paused: false } };
    await page.route('**/api/skills', async route => {
      const { agent, op, payload } = route.request().postDataJSON();
      if (!op.startsWith('autosync_')) return route.fallback();
      if (op === 'autosync_set') auto[agent] = { enabled: payload.enabled, state: payload.enabled ? 'idle' : 'stopping', paused: false };
      if (op === 'autosync_resume') auto[agent] = { enabled: true, state: 'idle', paused: false };
      return route.fulfill({ json: { ok: true, result: { ok: true, status: { agent, connected: null, ...auto[agent] } } } });
    });
    await section('设置');
    const toggle = ws.getByRole('switch', { name: '自动同步' });
    await toggle.click();
    await dialog.getByRole('button', { name: '取消' }).click();
    assert.equal(calls.filter(call => call.op === 'autosync_set').length, 0);
    await toggle.click();
    await dialog.getByRole('button', { name: '确认开启' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(calls.find(call => call.op === 'autosync_set'), { agent: 'codex', op: 'autosync_set', payload: { enabled: true, confirm: true } });
    await ws.getByRole('button', { name: 'Hermes', exact: true }).click();
    await section('设置');
    await wait(toggle);
    assert.equal(await toggle.isChecked(), false);
    await ws.getByRole('button', { name: 'Codex', exact: true }).click();
    auto.codex = { enabled: true, state: 'paused', paused: true };
    await ws.locator('.skills-context').getByRole('button', { name: '刷新' }).click();
    await ws.getByRole('button', { name: '恢复自动同步' }).click();
    await dialog.getByRole('button', { name: '确认恢复' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(calls.find(call => call.op === 'autosync_resume'), { agent: 'codex', op: 'autosync_resume', payload: { confirm: true } });
    await toggle.click();
    await wait(ws.getByText('正在结束当前一轮'));
    assert.equal(await toggle.isEnabled(), false);
    assert.deepEqual(calls.filter(call => call.op === 'autosync_set').at(-1).payload, { enabled: false });

    await page.route('**/api/skills', async route => {
      const { op, payload } = route.request().postDataJSON();
      if (op !== 'translate_descriptions') return route.fallback();
      assert.ok(payload.texts.every(value => typeof value === 'string'));
      return route.fulfill({ json: { ok: true, result: { translations: { 'Browser regression fixture': '浏览器回归测试技能' } } } });
    });
    const translate = ws.getByRole('switch', { name: '自动翻译' });
    const upstream = ws.getByRole('switch', { name: '自动更新' });
    await wait(ws.getByText('每天首次启动看板时检查上游，更新未修改的受管技能。'));
    await translate.click();
    await dialog.getByRole('button', { name: '确认开启' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(await translate.isChecked(), true);
    assert.equal(await upstream.isChecked(), false);
    await page.waitForFunction(() => document.activeElement?.id === 'skillsTranslate');
    if (process.env.SKILLS_QA_DIR) {
      await page.screenshot({ path: path.join(process.env.SKILLS_QA_DIR, 'skill-automation-settings.png'), fullPage: true });
      await page.setViewportSize({ width: 1280, height: 760 });
      await page.screenshot({ path: path.join(process.env.SKILLS_QA_DIR, 'skill-automation-desktop.png'), fullPage: true });
      await page.setViewportSize({ width: 375, height: 722 });
    }
    await section('技能库');
    await wait(ws.getByText('浏览器回归测试技能', { exact: true }));
    await ws.getByLabel('搜索技能').fill('回归测试');
    const beta = ws.getByRole('button', { name: '查看 catalog-beta 详情' });
    await beta.click();
    await wait(dialog.getByText('浏览器回归测试技能', { exact: true }));
    await dialog.getByText('简介原文').click();
    await wait(dialog.getByText('Browser regression fixture', { exact: true }));

    const git = path.join(input, '.git');
    await fs.mkdir(git);
    await fs.writeFile(path.join(git, 'config'), '[remote "origin"]\n  url = https://github.com/fixture-user/skills.git\n');
    await fs.writeFile(path.join(git, 'HEAD'), 'ref: refs/heads/main\n');
    await page.route(`${url}/api/skills`, async route => {
      const { op, payload } = route.request().postDataJSON();
      if (op !== 'github_discover') return route.fallback();
      const external = payload.id.startsWith('external:');
      await route.fulfill({ json: { ok: true, result: {
        status: external ? 'ambiguous' : 'request_failed', matches: [], preview_id: null,
        suggestions: external ? [
          { url: 'https://github.com/example/skills', ref: 'main', subdir: 'skills/sample',
            evidence: ['documented_path', 'name'], comparison: 'different' },
          { url: 'https://github.com/other/sample', subdir: 'different-skill',
            evidence: ['name'], fork: true, comparison: 'different' },
        ] : [],
        errors: external ? [] : ['github_rate_limited'], incomplete: !external,
      } } });
    });
    await dialog.getByRole('button', { name: '查找 GitHub 来源' }).click();
    await wait(dialog.getByText('GitHub 请求达到限额，稍后可以重试。'));
    await dialog.getByRole('button', { name: '手动指定来源' }).click();
    assert.equal(await dialog.getByLabel('GitHub 仓库或技能链接').inputValue(), 'https://github.com/fixture-user/skills');
    assert.equal(await dialog.getByLabel('子目录（可选）').inputValue(), 'catalog-beta');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.keyboard.press('Escape');
    await section('外部');
    const externalCard = ws.locator('.skills-card').filter({
      has: page.getByRole('button', { name: `查看 ${names.codex} 详情` }),
    });
    await externalCard.getByRole('button', { name: '查找 GitHub 来源' }).click();
    await wait(dialog.getByText('找到多个候选，请按仓库和子目录核对。'));
    await wait(dialog.getByText('文档明确指向此技能目录', { exact: false }));
    assert.equal(await dialog.getByRole('button', { name: '确认关联' }).count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.SKILLS_QA_DIR) {
      await page.screenshot({ path: path.join(process.env.SKILLS_QA_DIR, 'skill-upstream-narrow.png') });
      await page.setViewportSize({ width: 1120, height: 720 });
      await page.screenshot({ path: path.join(process.env.SKILLS_QA_DIR, 'skill-upstream-desktop.png') });
    }
    await dialog.getByRole('button', { name: '手动核对' }).first().click();
    assert.equal(await dialog.locator('input[name="url"]').inputValue(), 'https://github.com/example/skills');
    await page.keyboard.press('Escape');
    await ws.getByRole('button', { name: 'Hermes', exact: true }).click();
    await section('设置');
    await wait(translate);
    assert.equal(await translate.isChecked(), false);
    assert.ok(calls.every(call => !call.op.includes('.')));
    assert.deepEqual(errors, []);
    console.log('Skills UI browser regression passed (local integration + simulated sync/GitHub).');
  } finally {
    try { if (browser) await browser.close(); }
    finally {
      if (server && server.exitCode === null && server.signalCode === null) {
        const exited = once(server, 'exit');
        server.kill();
        await exited;
      }
      const resolved = await fs.realpath(root);
      if (path.dirname(resolved) === temp && path.basename(resolved).startsWith('dashboard-skills-ui-')) {
        await fs.rm(resolved, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
      }
    }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
