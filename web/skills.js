(() => {
  'use strict';

  const agents = { codex: 'Codex', hermes: 'Hermes' };
  const views = { library: '技能库', installed: '已装', external: '外部', market: '市场', backup: '备份', settings: '设置' };
  const sourceNames = { local: '本地', archive: '压缩包', github: 'GitHub', git: 'Git', market: 'skills.sh', 'skills.sh': 'skills.sh', external: '外部', imported: '已导入', builtin: '内置' };
  const importModes = { local: ['本地目录', '路径', '技能目录或包含技能的上级目录'], archive: ['压缩包', '压缩包路径', '服务主机上的 .zip 或 .skill 文件'], github: ['GitHub', '仓库地址', 'https://github.com/owner/repo'] };
  const paths = {
    layers: 'M12 2 2 7l10 5 10-5-10-5ZM2 12l10 5 10-5M2 17l10 5 10-5',
    terminal: 'M4 4h16v16H4zM8 9l3 3-3 3m5 0h3',
    hermes: 'M12 2 5 13h6l-1 9 9-12h-6l1-8',
    plus: 'M12 5v14M5 12h14', search: 'M21 21l-4.4-4.4M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
    refresh: 'M20 7v5h-5M4 17v-5h5m11-.5a8 8 0 0 0-14-5m-2 6a8 8 0 0 0 14 5',
    close: 'm6 6 12 12M18 6 6 18', package: 'm12 3 9 5v8l-9 5-9-5V8l9-5Zm0 10 9-5m-9 5L3 8m9 5v8',
    arrow: 'm10 7 5 5-5 5', clock: 'M12 7v5l3 2m6-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
    download: 'M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4', check: 'm5 12 4 4L19 6',
    tag: 'M3 5v6l10 10 8-8L11 3H5a2 2 0 0 0-2 2Zm5 2h.01',
    archive: 'M3 4h18v5H3zM5 9v11h14V9m-9 4h4'
  };
  const icon = name => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]}"/></svg>`;
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const esc = value => text(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
  const list = value => Array.isArray(value) ? value : [];
  const idOf = item => text(item?.id ?? item?.skill_id);
  const presetName = item => text(item?.name);
  const nameOf = item => text(item?.name) || idOf(item);
  const tagsOf = item => list(item?.tags).filter(tag => typeof tag === 'string' && tag);
  const isExternal = item => item?.external === true || item?.managed === false || item?.status === 'external' || item?.source === 'external';
  const mergeExternal = data => {
    const external = list(data.external ?? data.external_skills).map(item => ({ ...item, external: true }));
    return [...data.skills, ...external.filter(item => !data.skills.some(skill => isExternal(skill) && idOf(skill) === idOf(item)))];
  };
  const isInstalled = item => item?.installed === true;
  const canInstall = item => !isInstalled(item) && !list(item?.install_conflicts).length;
  const canDisable = item => !isExternal(item) && isInstalled(item) && item.installed_externally !== true;
  const installedSkills = skills => {
    const importedPaths = new Set(skills.filter(item => item.installed_externally).map(item => item.installed_path));
    return skills.filter(item => isInstalled(item) && !(isExternal(item) && importedPaths.has(item.installed_path)));
  };
  const sourceOf = item => {
    if (isExternal(item)) return 'external';
    const source = item?.source;
    const value = text(source?.type ?? source?.kind ?? source);
    return !value || /^([a-z]:[\\/]|[\\/])/i.test(value) ? 'local' : value;
  };
  const sourceLabel = source => sourceNames[source] ?? source;
  const rootPath = root => text(root?.path ?? root?.install_root ?? root?.resolved ?? root?.root ?? root);
  const dateLabel = value => {
    if (value == null || value === '') return '';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? text(value) : date.toLocaleString('zh-CN', { hour12: false });
  };
  const btn = (label, action, { value = '', glyph = '', tone = '', disabled = false, title = '' } = {}) => `<button type="button" class="skills-btn${tone ? ` skills-${tone}` : ''}" data-skill-action="${esc(action)}" data-value="${esc(value)}"${title ? ` title="${esc(title)}"` : ''}${disabled ? ' disabled' : ''}>${glyph ? icon(glyph) : ''}<span>${esc(label)}</span></button>`;
  const empty = (title, detail = '', action = '') => `<div class="skills-empty"><span class="skills-empty-icon">${icon('layers')}</span><h3>${esc(title)}</h3>${detail ? `<p>${esc(detail)}</p>` : ''}${action}</div>`;
  const note = (value, role = 'status') => value ? `<p class="skills-notice${role === 'alert' ? ' skills-notice-error' : ''}" role="${role}">${esc(value)}</p>` : '';
  const loading = value => `<p class="skills-loading" role="status">${esc(value)}</p>`;
  const commonErrors = {
    invalid_body: '请求格式不正确。', invalid_operation_or_payload: '当前服务不接受此操作或参数。',
    invalid_payload: '操作参数不正确。', sync_conflict: '本地和远端都有变更，未覆盖任何一侧。',
    sync_conflict_stale: '版本已变化，请重新核对后再选择处理方式。',
    preview_expired: '预览已过期，请重新预览。', update_expired: '更新预览已过期，请重新打开预览。',
    connection_changed: '仓库连接已更改，请重新确认是否开启自动同步。', sync_not_connected: '尚未连接备份仓库。',
    autosync_io: '自动同步状态读写失败，请检查本机目录权限。', remote_ahead: '远端有新版本。',
    'The local files differ from this GitHub version. Check the branch and skill folder.': '本地文件与该 GitHub 版本不一致，请核对分支和技能目录。',
    'A Codex skill with the same name already exists in the global skills directory. Refresh and check the conflicting paths in the skill details.': 'Codex 全局目录已有同名技能。刷新后，在技能详情中查看冲突路径。'
  };
  function apiMessage(error) {
    const message = text(error?.message ?? error?.error ?? error);
    return commonErrors[message] || message || commonErrors[error?.code] || '技能服务未返回错误详情。';
  }
  function unwrap(response, body) {
    const rejected = (error, context) => {
      const code = context?.code ?? error?.code;
      return Object.assign(new Error(commonErrors[code] || apiMessage(error)), { code, conflict: context?.conflict, status: context?.status });
    };
    if (!response.ok || body?.ok !== true) throw rejected(body?.result?.error ?? body?.error ?? body?.message ?? `请求失败（HTTP ${response.status}）`, body?.result ?? body);
    const result = Object.hasOwn(body, 'result') ? body.result : body;
    if (result?.ok === false) throw rejected(result.error ?? result, result);
    return result;
  }
  async function request(agent, op, payload = {}) {
    if (!Object.hasOwn(agents, agent)) throw new Error('未知的技能工作区。');
    let response;
    try {
      response = await fetch('/api/skills', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Dashboard-Request': '1' },
        body: JSON.stringify({ agent, op, payload })
      });
    } catch (_) { throw new Error('无法连接本地技能服务，请检查看板服务后重试。'); }
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error(`本地技能服务未返回 JSON（HTTP ${response.status}）。`); }
    return unwrap(response, body);
  }

  const workspaces = Object.fromEntries(Object.keys(agents).map(agent => [agent, {
    agent, view: 'library', data: null, busy: '', loadError: '', notice: '', error: '',
    query: '', source: '', tag: '', filter: 'all', preset: '', updates: new Map(), selection: new Set(),
    market: { query: '', board: 'hot', items: null, pending: false, error: '' },
    backups: null, sync: null, conflict: null, autoSync: null, resolvedConflict: '', rootInfo: null, readErrors: {}, pending: new Set(),
    automation: null, translations: new Map(), translationError: '', translationRetry: 0
  }]));
  let active = 'codex';
  let host;
  let dialog;
  let modal;
  let modalReturn;
  let opened = false;
  const current = () => workspaces[active];
  const selectedSkill = (ws, id, external = false) => ws.data?.skills.find(skill => idOf(skill) === id && isExternal(skill) === external);
  const say = message => { document.getElementById('skillsAnnouncement').textContent = message; };
  function paint(ws) { if (ws === current() && host) render(); }
  const translationErrors = {
    translation_key_missing: '未检测到 DEEPL_AUTH_KEY。配置环境变量并重启看板后重试。',
    translation_auth: 'DeepL 未接受 API 密钥，请检查 DEEPL_AUTH_KEY。',
    translation_quota: 'DeepL 翻译额度已用完，当前显示原文。',
    translation_unavailable: '暂时无法连接 DeepL，当前显示原文。'
  };
  const translationEnabled = ws => ws.data?.automation?.translate === true;
  const originalOf = item => text(item.description).slice(0, 2000);
  const descriptionOf = (ws, item) => translationEnabled(ws) && ws.translations.get(originalOf(item)) || text(item.description);
  const descriptionAttr = (ws, item) => translationEnabled(ws) && originalOf(item)
    ? ` data-skill-description="${esc(originalOf(item))}"` : '';
  const translationNodes = selector => [
    ...host.querySelectorAll(selector), ...(dialog?.open ? dialog.querySelectorAll(selector) : [])
  ];

  async function translateVisible(ws) {
    if (ws !== current() || !translationEnabled(ws) || ws.pending.has('translation') || Date.now() < ws.translationRetry) return;
    const nodes = translationNodes('[data-skill-description]');
    const texts = [...new Set(nodes.map(node => node.dataset.skillDescription))]
      .filter(value => value && !ws.translations.has(value)).slice(0, 16);
    if (!texts.length) return;
    ws.pending.add('translation');
    try {
      const result = await request(ws.agent, 'translate_descriptions', { texts });
      ws.translationError = translationErrors[result?.error] || '';
      if (result?.error) ws.translationRetry = Date.now() + 60000;
      for (const value of texts) {
        const translated = Object.hasOwn(result?.translations ?? {}, value) ? text(result.translations[value]) : '';
        if (translated || !result?.error) ws.translations.set(value, translated);
      }
    } catch (_) {
      ws.translationError = translationErrors.translation_unavailable;
      ws.translationRetry = Date.now() + 60000;
    } finally {
      ws.pending.delete('translation');
    }
    if (ws !== current() || !translationEnabled(ws)) return;
    for (const node of translationNodes('[data-skill-description]')) {
      const translated = ws.translations.get(node.dataset.skillDescription);
      if (translated) {
        node.textContent = translated;
        if (node.classList.contains('skills-card-desc')) node.title = node.dataset.skillDescription;
      }
    }
    for (const original of translationNodes('[data-skill-original]')) {
      original.hidden = !ws.translations.get(original.dataset.skillOriginal);
    }
    if (ws.view === 'settings') paint(ws);
    else if (!ws.translationError) void translateVisible(ws);
  }

  async function readAutomation(ws, quiet = false) {
    if (ws.pending.has('automation')) return;
    ws.pending.add('automation');
    const previous = ws.automation;
    const hadError = !!ws.readErrors.automation;
    try {
      ws.automation = await request(ws.agent, 'skill_automation');
      delete ws.readErrors.automation;
      const latest = ws.automation?.upstream;
      if (latest?.checked_at && previous?.upstream?.checked_at !== latest.checked_at && (latest.updated || latest.linked)) {
        await loadState(ws);
      }
    } catch (error) { ws.readErrors.automation = error.message; }
    finally {
      ws.pending.delete('automation');
      if (!quiet || hadError || ws.readErrors.automation || JSON.stringify(previous) !== JSON.stringify(ws.automation)) paint(ws);
    }
  }

  async function loadState(ws) {
    if (ws.pending.has('state')) return;
    ws.pending.add('state');
    ws.loadError = '';
    paint(ws);
    try {
      const data = await request(ws.agent, 'state');
      if (!Array.isArray(data?.skills)) throw new Error('技能列表格式不正确。');
      ws.data = { ...data, skills: mergeExternal(data), presets: list(data.presets) };
    } catch (error) { ws.loadError = error.message; }
    finally { ws.pending.delete('state'); paint(ws); }
  }

  function getResults(ws) {
    const skills = ws.data?.skills ?? [];
    const base = ws.view === 'installed' ? installedSkills(skills)
      : skills.filter(skill => ws.view === 'external' ? isExternal(skill) : !isExternal(skill));
    const options = {
      sources: [...new Set(base.map(sourceOf))].sort(),
      tags: [...new Set(base.flatMap(tagsOf))].sort((a, b) => a.localeCompare(b, 'zh-CN'))
    };
    const query = ws.query.trim().toLocaleLowerCase();
    return { ...options, items: base.filter(skill => {
      if (ws.source && options.sources.includes(ws.source) && sourceOf(skill) !== ws.source) return false;
      if (ws.tag === '__untagged' && tagsOf(skill).length) return false;
      if (ws.tag && ws.tag !== '__untagged' && options.tags.includes(ws.tag) && !tagsOf(skill).includes(ws.tag)) return false;
      if (ws.view === 'library' && ws.filter === 'available' && isInstalled(skill)) return false;
      if (ws.view === 'library' && ws.filter === 'updates' && !hasUpdate(ws, skill)) return false;
      return !query || [idOf(skill), nameOf(skill), text(skill.description), descriptionOf(ws, skill), sourceLabel(sourceOf(skill)), ...tagsOf(skill)].some(value => value.toLocaleLowerCase().includes(query));
    }) };
  }
  const hasUpdate = (ws, skill) => ws.updates.get(idOf(skill))?.status === 'available' || (!ws.updates.has(idOf(skill)) && skill.update_available === true);
  const option = (value, label, activeValue) => `<option value="${esc(value)}"${value === activeValue ? ' selected' : ''}>${esc(label)}</option>`;
  const tag = value => `<span class="skills-tag">${esc(value)}</span>`;
  const versionOf = item => text(item?.version ?? item?.version_id ?? item?.revision);
  const statusLabel = item => {
    if (isExternal(item)) return '外部 · 受保护';
    if (item.installed_externally) return '已装 · 受保护';
    if (isInstalled(item)) return list(item.install_conflicts).length ? '已装 · 同名冲突' : '已装';
    return canInstall(item) ? '未安装' : '同名冲突';
  };
  function card(ws, skill) {
    const id = idOf(skill);
    const name = nameOf(skill);
    const external = isExternal(skill);
    const installed = isInstalled(skill);
    const update = !external && hasUpdate(ws, skill);
    const version = versionOf(skill);
    const options = { value: id, disabled: !!ws.busy };
    let action = '';
    if (external) action = btn('导入库', 'adopt', { ...options, disabled: !text(skill.path) || !!ws.busy });
    else if (canDisable(skill)) action = btn('停用', 'disable', options);
    else if (canInstall(skill)) action = btn('安装', 'install', { ...options, tone: 'quiet-primary' });
    else if (list(skill.install_conflicts).length) action = btn('查看冲突', 'detail', options);
    const selector = ws.view === 'library' && !external ? `<label class="skills-card-select"><input type="checkbox" data-skill-selection="${esc(id)}" aria-label="选择 ${esc(name)}"${ws.selection.has(id) ? ' checked' : ''}${ws.busy ? ' disabled' : ''}></label>` : `<span class="skills-card-icon">${icon('package')}</span>`;
    return `<article class="skills-card">
      <div class="skills-card-body"><div class="skills-card-head">${selector}<h3><button type="button" class="skills-title-btn" data-skill-action="${external ? 'external-detail' : 'detail'}" data-value="${esc(id)}" aria-label="查看 ${esc(name)} 详情">${esc(name)}</button></h3><span class="skills-state${installed && !external ? ' is-installed' : ''}"><i aria-hidden="true"></i>${statusLabel(skill)}</span></div>
      <p class="skills-card-desc"${descriptionAttr(ws, skill)}${descriptionOf(ws, skill) !== text(skill.description) ? ` title="${esc(skill.description)}"` : ''}>${esc(descriptionOf(ws, skill) || '暂无描述')}</p>
      ${tagsOf(skill).length ? `<div class="skills-tags">${tagsOf(skill).slice(0, 4).map(tag).join('')}${tagsOf(skill).length > 4 ? tag(`+${tagsOf(skill).length - 4}`) : ''}</div>` : ''}</div>
      <div class="skills-card-foot"><span class="skills-card-meta" title="${esc(version)}">${esc(sourceLabel(sourceOf(skill)))}${version ? ` <span aria-hidden="true">·</span> ${esc(version)}` : ''}</span><div class="skills-actions">${update ? btn('可更新', 'update', { value: id, tone: 'warning', disabled: !!ws.busy }) : ''}${action}</div></div>
    </article>`;
  }
  function presets(ws) {
    const choices = ws.data.presets;
    const selected = choices.find(item => presetName(item) === ws.preset);
    const members = list(selected?.ids).map(id => selectedSkill(ws, id)).filter(Boolean);
    const busy = !!ws.busy;
    return `<div class="skills-presets"><label for="skillsPreset">预设</label><select id="skillsPreset">${option('', '选择预设', ws.preset)}${choices.map(item => option(presetName(item), `${presetName(item)} · ${list(item.ids).length} 项`, ws.preset)).join('')}</select>${btn('启用成员', 'preset-apply', { value: 'enable', disabled: busy || !members.some(canInstall) })}${btn('停用成员', 'preset-apply', { value: 'disable', disabled: busy || !members.some(canDisable) })}${btn('删除预设', 'preset-delete', { tone: 'danger-quiet', disabled: busy || !selected })}${btn('新建预设', 'preset-save', { glyph: 'plus', disabled: busy })}</div>`;
  }
  function renderList(ws) {
    if (!ws.data) return ws.pending.has('state') ? loading('正在读取技能工作区…') : '';
    const results = getResults(ws);
    return `<div class="skills-toolbar">
      <label class="skills-search">${icon('search')}<span class="skills-visually-hidden">搜索技能</span><input id="skillsSearch" type="search" value="${esc(ws.query)}" placeholder="搜索名称、说明或标签" autocomplete="off"></label>
      <label class="skills-visually-hidden" for="skillsSource">技能来源</label><select id="skillsSource">${option('', '全部来源', ws.source)}${results.sources.map(source => option(source, sourceLabel(source), ws.source)).join('')}</select>
      <label class="skills-visually-hidden" for="skillsTag">技能标签</label><select id="skillsTag">${option('', '全部标签', ws.tag)}${option('__untagged', '无标签', ws.tag)}${results.tags.map(value => option(value, value, ws.tag)).join('')}</select>
      ${ws.view === 'library' ? `<label class="skills-visually-hidden" for="skillsFilter">安装和更新状态</label><select id="skillsFilter">${option('all', '全部状态', ws.filter)}${option('available', '未安装', ws.filter)}${option('updates', '可更新', ws.filter)}</select>` : ''}
      ${ws.view === 'library' ? btn('检查更新', 'check-updates', { glyph: 'refresh', disabled: !!ws.busy || !ws.data.skills.some(item => !isExternal(item)) }) : ''}
    </div>
    ${ws.view !== 'external' ? presets(ws) : ''}
    <div id="skillsResults">${renderResults(ws, results.items)}</div>`;
  }
  function renderResults(ws, items = getResults(ws).items) {
    if (ws.view === 'library') {
      const visible = new Set(items.map(idOf));
      for (const id of ws.selection) if (!visible.has(id)) ws.selection.delete(id);
    }
    const filtered = ws.query || ws.source || ws.tag || (ws.view === 'library' && ws.filter !== 'all');
    if (!items.length) {
      if (filtered) return empty('没有匹配的技能', '', btn('清除筛选', 'clear-filters'));
      if (ws.view === 'external') return empty('未发现外部技能');
      if (ws.view === 'installed') return empty('当前工作区未安装技能', '', btn('查看技能库', 'view', { value: 'library' }));
      return empty('技能库为空', '', btn('导入技能', 'import', { glyph: 'plus', tone: 'primary' }));
    }
    return `<div class="skills-results-head"><p class="skills-result-count">${items.length} 项技能</p>${ws.view === 'library' ? `<div class="skills-actions" id="skillsBatch">${batchControls(ws, items)}</div>` : ''}</div><div class="skills-card-grid">${items.map(skill => card(ws, skill)).join('')}</div>`;
  }
  const selectedItems = (ws, items = getResults(ws).items) => items.filter(item => !isExternal(item) && ws.selection.has(idOf(item)));
  function batchControls(ws, items) {
    const selected = selectedItems(ws, items);
    const all = selected.length === items.length && items.length > 0;
    const disabled = !!ws.busy;
    return `${btn(all ? '取消全选' : '全选结果', 'batch-select', { disabled })}${selected.length ? `<span class="skills-count">已选 ${selected.length} 项</span>${btn('清空选择', 'batch-clear', { disabled })}${btn('批量安装', 'batch-install', { disabled: disabled || !selected.some(canInstall) })}${btn('批量停用', 'batch-disable', { disabled: disabled || !selected.some(canDisable) })}${btn('批量删除', 'batch-delete', { tone: 'danger-quiet', disabled })}` : ''}`;
  }
  function paintSelection(ws) {
    const toolbar = host.querySelector('#skillsBatch');
    if (!toolbar) return;
    const previous = toolbar.contains(document.activeElement) ? document.activeElement : null;
    toolbar.innerHTML = batchControls(ws, getResults(ws).items);
    host.querySelectorAll('[data-skill-selection]').forEach(input => { input.checked = ws.selection.has(input.dataset.skillSelection); });
    if (previous) (focusKey(previous) || toolbar.querySelector('button'))?.focus({ preventScroll: true });
    say(`已选择 ${ws.selection.size} 项技能。`);
  }

  function renderMarket(ws) {
    const market = ws.market;
    return `<form class="skills-toolbar" data-skill-form="market">
      <label class="skills-search">${icon('search')}<span class="skills-visually-hidden">搜索市场</span><input name="query" type="search" maxlength="200" value="${esc(market.query)}" placeholder="搜索 skills.sh 市场" autocomplete="off"></label>
      <label class="skills-visually-hidden" for="skillsMarketBoard">市场榜单</label><select id="skillsMarketBoard" name="board">${option('hot', '热门', market.board)}${option('trending', '趋势', market.board)}${option('all', '总榜', market.board)}</select>
      <button type="submit" class="skills-btn skills-primary">搜索市场</button>
    </form>${market.error ? note(market.error, 'alert') : ''}${market.pending ? loading('正在读取市场…') : market.items === null ? '' : market.items.length ? `<p class="skills-result-count">${market.items.length} 条结果</p><div class="skills-card-grid">${market.items.map((item, i) => `<article class="skills-card skills-market-card"><div class="skills-card-head"><span class="skills-card-icon">${icon('package')}</span><h3>${esc(nameOf(item))}</h3></div><p>${esc(item.source || 'skills.sh')}${Number.isFinite(item.installs) ? ` <span aria-hidden="true">·</span> ${item.installs.toLocaleString('zh-CN')} 次安装` : ''}</p>${btn('预览导入', 'market-import', { value: i, glyph: 'arrow', disabled: !item.url || !!ws.busy })}</article>`).join('')}</div>` : empty('没有匹配的市场技能')}`;
  }

  const syncLabels = { disconnected: '未连接', unknown: '远端状态未检查', in_sync: '已同步', local_changes: '本地有变更', remote_ahead: '远端有新版本', conflict: '存在冲突' };
  const reasons = { manual: '手动备份', before_restore: '恢复前保护备份', before_pull: '拉取前保护备份', conflict_local: '冲突处理前 · 本地备份', conflict_remote: '冲突处理前 · 远端备份' };
  const activitySection = () => `<section class="skills-section"><header class="skills-section-head"><div><h2>活动日志</h2></div><div class="skills-actions">${btn('查看日志', 'activity', { glyph: 'clock' })}${btn('导出日志', 'activity-export', { glyph: 'download' })}</div></header></section>`;
  const githubButton = ws => btn('通过 GitHub 连接', 'github-connect', { disabled: !!ws.busy || ws.sync?.git_available === false });
  function renderBackup(ws) {
    const sync = ws.sync;
    return `<section class="skills-section"><header class="skills-section-head"><div><h2>本地备份</h2><p>${agents[ws.agent]} 工作区的备份与恢复</p></div>${btn('创建备份', 'backup-create', { glyph: 'archive', tone: 'primary', disabled: !!ws.busy })}</header>
      ${note(ws.readErrors.backup_list, 'alert')}${ws.backups ? ws.backups.length ? `<ul class="skills-records">${ws.backups.map(item => `<li><div><strong>${esc(reasons[item.reason] || '备份')}</strong><span>${esc([dateLabel(item.created_at), item.backup_id, Number.isFinite(item.file_count) ? `${item.file_count} 个文件` : ''].filter(Boolean).join(' · '))}</span></div>${btn('恢复', 'backup-restore', { value: item.backup_id, disabled: !!ws.busy })}</li>`).join('')}</ul>` : '<p class="skills-muted">暂无备份记录</p>' : ws.pending.has('backup_list') ? loading('正在读取备份…') : ''}</section>
      <section class="skills-section"><header class="skills-section-head"><div><h2>远程同步</h2><p>${esc(sync ? syncLabels[sync.state] || (sync.connected ? '已连接' : '未连接') : '同步状态')}</p></div><div class="skills-actions">${githubButton(ws)}${btn(sync?.connected ? '手动更换连接' : '手动连接', 'sync-connect', { disabled: !!ws.busy || !sync || sync.git_available === false })}</div></header>
      ${note(ws.readErrors.sync_status, 'alert')}${sync ? `<dl class="skills-meta"><div><dt>远程仓库</dt><dd>${esc(sync.remote_url || '未配置')}</dd></div><div><dt>分支</dt><dd>${esc(sync.branch || '—')}</dd></div><div><dt>上次同步</dt><dd>${esc(dateLabel(sync.last_synced_at) || '—')}</dd></div></dl>${sync.git_available === false ? note('本机未找到 Git，暂不能同步。') : ''}${sync.connected ? `<div class="skills-actions">${btn('推送到远端', 'sync-push', { disabled: !!ws.busy || sync.git_available === false })}${btn('从远端拉取', 'sync-pull', { disabled: !!ws.busy || sync.git_available === false })}${ws.conflict ? btn('处理冲突', 'sync-conflict', { tone: 'warning' }) : ''}</div>` : ''}` : ws.pending.has('sync_status') ? loading('正在读取同步状态…') : ''}</section>${activitySection()}`;
  }

  function renderSettings(ws) {
    const root = rootPath(ws.rootInfo ?? ws.data?.root);
    return `<section class="skills-section"><header class="skills-section-head"><div><h2>全局技能目录</h2><p>当前工作区：${agents[ws.agent]}</p></div>${btn('读取目录', 'read-root', { glyph: 'refresh', disabled: ws.pending.has('root') })}</header>
      ${note(ws.readErrors.root, 'alert')}<dl class="skills-meta"><div><dt>安装路径</dt><dd><code>${esc(root || '未读取到路径')}</code></dd></div></dl>
      <div class="skills-actions">${btn('更改目录', 'edit-root', { disabled: !!ws.busy || !root })}${btn('恢复默认目录', 'reset-root', { disabled: !!ws.busy || !root })}</div></section>${renderAutomation(ws)}<section class="skills-section"><header class="skills-section-head"><h2>备份仓库</h2><div class="skills-actions">${githubButton(ws)}${btn('查看备份与同步', 'view', { value: 'backup' })}</div></header></section>${renderAutoSync(ws)}${activitySection()}`;
  }

  function renderAutomation(ws) {
    const state = ws.automation;
    const settings = state?.settings ?? ws.data?.automation ?? {};
    const upstream = state?.upstream;
    const toggle = (id, label, checked, disabled) => `<label class="skills-auto-toggle"><input type="checkbox" role="switch" id="${id}"${checked ? ' checked' : ''}${disabled ? ' disabled' : ''}>${label}</label>`;
    return `<section class="skills-section">
      <header class="skills-section-head"><div><h2>简介翻译</h2><p>通过 DeepL 翻译简介，译文保存在本机。</p></div>${toggle('skillsTranslate', '自动翻译', settings.translate, !state || !!ws.busy || !state.key_available && !settings.translate)}</header>
      ${state?.key_available === false ? note(translationErrors.translation_key_missing) : ''}
      ${ws.translationError && state?.key_available ? note(ws.translationError, 'alert') : ''}
    </section><section class="skills-section">
      <header class="skills-section-head"><div><h2>GitHub 技能更新</h2><p>每天首次启动看板时检查上游，更新未修改的受管技能。</p></div>${toggle('skillsGitHubAuto', '自动更新', settings.github_auto, !state || !!ws.busy)}</header>
      ${note(ws.readErrors.automation, 'alert')}
      ${settings.github_auto && upstream?.checked_at ? `<dl class="skills-meta"><div><dt>上次检查</dt><dd>${esc(dateLabel(upstream.checked_at))}</dd></div><div><dt>检查结果</dt><dd>关联 ${esc(upstream.linked ?? 0)} 项，更新 ${esc(upstream.updated ?? 0)} 项，跳过 ${esc(upstream.skipped ?? 0)} 项，失败 ${esc(upstream.failed ?? 0)} 项</dd></div></dl>` : ''}
    </section>`;
  }

  const autoStates = { disabled: '已关闭', idle: '等待变更', debouncing: '等待本地变更结束', pushing: '正在推送', pulling: '正在拉取', retry_wait: '等待重试', paused_conflict: '因冲突暂停', paused: '已暂停', stopping: '正在结束当前一轮', stopped: '已停止' };
  const conflictKey = conflict => `${text(conflict?.remote_revision)}:${text(conflict?.local_digest)}`;
  const syncConnected = ws => ws.sync?.connected ?? ws.autoSync?.connected;
  function renderAutoSync(ws) {
    const state = ws.autoSync;
    const connected = syncConnected(ws);
    const enabled = state?.enabled === true;
    const editable = !!state && !ws.busy && (enabled || connected === true && state.state !== 'stopping');
    const canResolve = conflictValid(state?.conflict) && conflictKey(state.conflict) !== ws.resolvedConflict;
    const hint = state ? autoStates[state.state] ?? (enabled ? '已开启' : '已关闭') : '状态未读取';
    return `<section class="skills-section"><header class="skills-section-head"><div><h2>自动同步</h2><p>${hint}</p></div>${state ? `<label class="skills-auto-toggle"><input type="checkbox" role="switch" id="skillsAutoSync"${enabled ? ' checked' : ''}${editable ? '' : ' disabled'}>自动同步</label>` : ''}</header>
      ${note(ws.readErrors.autosync_status, 'alert')}${note(ws.readErrors.sync_status, 'alert')}${!state && ws.pending.has('autosync_status') ? loading('正在读取自动同步状态…') : ''}
      ${state && !enabled && connected === false ? '<p class="skills-muted">尚未连接备份仓库。</p>' : ''}
      ${state?.last_success_at || Number.isFinite(state?.next_in_seconds) ? `<dl class="skills-meta">${state.last_success_at ? `<div><dt>上次成功</dt><dd>${esc(dateLabel(state.last_success_at))}</dd></div>` : ''}${Number.isFinite(state.next_in_seconds) ? `<div><dt>下轮检查</dt><dd>${state.next_in_seconds} 秒后</dd></div>` : ''}</dl>` : ''}
      ${state?.paused && state.pause_reason === 'connection_changed' && !state.last_error ? note(commonErrors.connection_changed) : ''}
      ${state?.last_error ? note(apiMessage(state.last_error), 'alert') : ''}
      ${state?.paused || ws.readErrors.autosync_status ? `<div class="skills-actions">${canResolve ? btn('处理冲突', 'autosync-conflict', { tone: 'warning' }) : ''}${state?.paused ? btn('恢复自动同步', 'autosync-resume', { disabled: !enabled || connected !== true || canResolve }) : ''}${ws.readErrors.autosync_status ? btn('重试读取', 'autosync-refresh') : ''}</div>` : ''}
    </section>`;
  }

  function focusKey(element) {
    if (!element) return undefined;
    if (element.id) return [...host.querySelectorAll('[id]')].find(item => item.id === element.id);
    const data = element.dataset;
    if (data?.skillSelection) return [...host.querySelectorAll('[data-skill-selection]')].find(item => item.dataset.skillSelection === data.skillSelection && !item.disabled);
    if (data?.skillAction) return [...host.querySelectorAll('[data-skill-action]')].find(item => item.dataset.skillAction === data.skillAction && item.dataset.value === data.value && !item.disabled);
    return undefined;
  }
  function render() {
    const ws = current();
    const previous = host.contains(document.activeElement) ? document.activeElement : null;
    const form = host.querySelector('[data-skill-form="market"]');
    const draftQuery = form?.elements.query.value;
    const visible = ws.data?.skills ?? [];
    const counts = { library: visible.filter(skill => !isExternal(skill)).length, installed: installedSkills(visible).length, external: visible.filter(isExternal).length };
    const root = rootPath(ws.data?.root ?? ws.rootInfo);
    host.innerHTML = `<div class="skills-context">
      <div class="skills-context-name"><span class="skills-agent-icon">${icon(active === 'codex' ? 'terminal' : 'hermes')}</span><div><h2>${agents[active]} 全局工作区</h2>${root ? `<code title="${esc(root)}">${esc(root)}</code>` : ''}</div></div>
      <div class="skills-context-tools"><div class="skills-segment" role="group" aria-label="全局工作区">${Object.entries(agents).map(([key, label]) => `<button type="button" data-skill-action="agent" data-value="${key}" aria-pressed="${key === active}">${label}</button>`).join('')}</div>${btn('刷新', 'refresh', { glyph: 'refresh', disabled: ws.pending.has('state') || !!ws.busy })}${btn('导入技能', 'import', { glyph: 'plus', tone: 'primary', disabled: !!ws.busy })}</div>
    </div>
    <nav class="skills-nav" aria-label="技能管理页面">${Object.entries(views).map(([key, label]) => `<button type="button" data-skill-action="view" data-value="${key}" ${key === ws.view ? 'aria-current="page"' : ''}><span>${label}</span>${ws.data && key in counts ? `<span class="skills-count">${counts[key]}</span>` : ''}</button>`).join('')}</nav>
    ${ws.loadError ? `<div class="skills-retry">${note(`读取工作区失败：${ws.loadError}`, 'alert')}${btn('重试', 'refresh')}</div>` : ''}
    ${note(ws.error, 'alert')}${!ws.busy ? note(ws.notice) : loading(ws.busy)}
    ${ws.view === 'market' ? renderMarket(ws) : ws.view === 'backup' ? renderBackup(ws) : ws.view === 'settings' ? renderSettings(ws) : renderList(ws)}`;
    if (previous && !dialog?.open) {
      const replacement = focusKey(previous) || (previous.dataset.skillAction === 'view' ? host.querySelector('[aria-current="page"]') : null);
      if (replacement) replacement.focus({ preventScroll: true });
      else if (previous.name === 'query' && draftQuery != null) {
        const input = host.querySelector('[data-skill-form="market"] [name="query"]');
        if (input) { input.value = draftQuery; input.focus({ preventScroll: true }); }
      }
    }
    void translateVisible(ws);
  }

  function showModal(title, content, { submit, label = '确认', danger = false, opener = document.activeElement } = {}) {
    if (!dialog.open) {
      modalReturn = opener;
      dialog.showModal();
    }
    modal = { ws: current(), submit, busy: false, version: Symbol() };
    dialog.innerHTML = `<form class="skills-dialog-form"><header><h2 id="skillsDialogTitle" tabindex="-1">${esc(title)}</h2>${btn('关闭', 'close', { glyph: 'close' })}</header><div class="skills-dialog-content">${content}<p class="skills-notice skills-notice-error" data-dialog-error role="alert" hidden></p></div>${submit ? `<footer>${btn('取消', 'close')}<button type="submit" class="skills-btn skills-${danger ? 'danger' : 'primary'}">${esc(label)}</button></footer>` : ''}</form>`;
    (submit && !danger && dialog.querySelector('input:not([type="checkbox"]), select, textarea') || dialog.querySelector('#skillsDialogTitle')).focus();
    void translateVisible(current());
    return modal.version;
  }
  function modalError(error) {
    const region = dialog.querySelector('[data-dialog-error]');
    region.textContent = error.message || String(error);
    region.hidden = false;
  }
  function confirmAction(title, message, task, options = {}) {
    showModal(title, `<p>${esc(message)}</p>`, { submit: task, ...options });
  }
  function field(title, name, { type = 'text', value = '', hint = '', required = false, maxlength = 1024 } = {}) {
    return `<label class="skills-field"><span>${esc(title)}</span><input type="${type}" name="${name}" value="${esc(value)}"${hint ? ` placeholder="${esc(hint)}"` : ''}${required ? ' required' : ''} maxlength="${maxlength}" autocomplete="off"></label>`;
  }
  function fieldValue(form, key) { return text(form.get(key)).trim(); }
  function validRemote(url) {
    if (/^[a-z\d_.-]+@[a-z\d.-]+:[^\s]+$/i.test(url)) return true;
    try {
      const parsed = new URL(url);
      return !!parsed.hostname && !parsed.password && !parsed.search && !parsed.hash && (parsed.protocol === 'https:' && !parsed.username || parsed.protocol === 'ssh:');
    } catch (_) { return false; }
  }
  async function change(ws, op, payload, success, refresh = true) {
    const result = await request(ws.agent, op, payload);
    ws.notice = typeof success === 'function' ? success(result) : success;
    ws.error = '';
    if (refresh) await loadState(ws);
    if (ws === current()) say(ws.notice || '操作完成。');
    paint(ws);
    return result;
  }

  async function readSide(ws, op, quiet = false) {
    if (ws.pending.has(op)) return;
    ws.pending.add(op);
    const hadError = !!ws.readErrors[op];
    delete ws.readErrors[op];
    if (!quiet) paint(ws);
    const previous = ws.autoSync;
    try {
      const result = await request(ws.agent, op);
      if (op === 'backup_list') ws.backups = list(result?.backups ?? result);
      else if (op === 'sync_status') ws.sync = result?.status ?? result;
      else if (op === 'root') ws.rootInfo = result?.root ?? result;
      else if (op === 'autosync_status') {
        if (result?.status?.agent && result.status.agent !== ws.agent) throw new Error('服务返回的自动同步状态不属于当前工作区。');
        ws.autoSync = result?.status ?? result;
      }
    } catch (error) { ws.readErrors[op] = error.message; }
    finally {
      ws.pending.delete(op);
      if (!quiet || hadError || ws.readErrors[op] || JSON.stringify(previous) !== JSON.stringify(ws.autoSync)) paint(ws);
    }
  }
  async function searchMarket(ws) {
    const market = ws.market;
    const ticket = Symbol();
    market.ticket = ticket;
    market.pending = true;
    market.error = '';
    paint(ws);
    try {
      const result = await request(ws.agent, 'market', { query: market.query, board: market.board, limit: 30 });
      if (market.ticket === ticket) market.items = list(result?.items ?? result);
    } catch (error) { if (market.ticket === ticket) market.error = error.message; }
    finally { if (market.ticket === ticket) { market.pending = false; paint(ws); } }
  }
  function refreshView(ws) {
    if (ws.view === 'backup') {
      readSide(ws, 'backup_list');
      readSide(ws, 'sync_status');
    } else if (ws.view === 'settings') {
      readSide(ws, 'root');
      readSide(ws, 'sync_status');
      readSide(ws, 'autosync_status');
      void readAutomation(ws);
    }
    else if (ws.view === 'market') searchMarket(ws);
  }

  function showImport(ws, mode = 'local', values = {}) {
    const [, fieldName, hint] = importModes[mode];
    const remote = mode === 'github';
    showModal(`导入到 ${agents[ws.agent]} 技能库`, `<div class="skills-segment" role="group" aria-label="导入来源">${Object.entries(importModes).map(([key, [label]]) => `<button type="button" data-skill-action="import-mode" data-value="${key}" aria-pressed="${key === mode}">${label}</button>`).join('')}</div>
      ${field(fieldName, remote ? 'url' : 'path', { hint, value: values[remote ? 'url' : 'path'], required: true })}
      ${remote ? `<div class="skills-form-columns">${field('分支或标签（可选）', 'ref', { value: values.ref })}${field('子目录（可选）', 'subdir', { value: values.subdir })}</div>` : ''}`, {
      label: '预览技能', submit: form => {
        const payload = remote ? { url: fieldValue(form, 'url') } : { path: fieldValue(form, 'path') };
        for (const key of ['ref', 'subdir']) if (fieldValue(form, key)) payload[key] = fieldValue(form, key);
        return showCandidates(ws, mode, payload);
      }
    });
  }
  async function showCandidates(ws, mode, payload, stillOpen = () => true) {
    const operation = `${mode}_preview`;
    const result = await request(ws.agent, operation, payload);
    if (!stillOpen()) return;
    const candidates = list(result?.candidates).filter(item => idOf(item));
    const title = `导入预览 · ${agents[ws.agent]}`;
    if (!candidates.length) {
      showModal(title, `<p>没有发现可导入的技能。</p>${btn('返回导入', 'import-mode', { value: mode })}`);
      return;
    }
    if (!result?.preview_id) throw new Error('技能服务未返回有效的导入预览。');
    const rows = candidates.map((item, index) => `<label class="skills-check-row"><input name="candidate" type="checkbox" value="${index}"><span><strong>${esc(nameOf(item))}</strong><small${descriptionAttr(ws, item)}>${esc(descriptionOf(ws, item) || item.path || '')}</small>${item.path && item.description ? `<small>${esc(item.path)}</small>` : ''}</span></label>`).join('');
    showModal(title, `<div class="skills-section-head"><p>发现 ${candidates.length} 项技能</p>${btn('全选', 'select-all', { value: 'candidate' })}</div><div class="skills-check-list">${rows}</div>`, {
      label: '导入所选技能', submit: async form => {
        const ids = form.getAll('candidate').map(index => idOf(candidates[Number(index)])).filter(Boolean);
        if (!ids.length) throw new Error('请至少选择一项技能。');
        const imported = await change(ws, 'import_selected', { preview_id: result.preview_id, ids }, data => `已导入 ${list(data?.imported).length} 项，失败 ${list(data?.errors).length} 项。`);
        const failures = list(imported?.errors);
        if (failures.length) showModal('导入结果', `<p>${esc(ws.notice)}</p><ul class="skills-records">${failures.map(item => `<li><div><strong>${esc(nameOf(candidates.find(entry => idOf(entry) === idOf(item))) || idOf(item))}</strong><span>${esc(apiMessage(item.error ?? item))}</span></div></li>`).join('')}</ul>${btn('重新预览', 'import-mode', { value: mode })}`);
      }
    });
  }
  async function modalRead(ws, title, task, pendingMessage = '正在读取…') {
    const token = showModal(title, loading(pendingMessage));
    try { await task(() => dialog.open && modal?.version === token && modal?.ws === ws); }
    catch (error) { if (dialog.open && modal?.version === token) { dialog.querySelector('.skills-loading')?.remove(); modalError(error); } }
  }

  function showDetail(ws, skill, external) {
    const id = idOf(skill);
    const title = `${nameOf(skill)} · 技能详情`;
    return modalRead(ws, title, async stillOpen => {
      const result = await request(ws.agent, 'detail', { id });
      if (!stillOpen()) return;
      const item = { ...skill, ...(result?.skill ?? result?.detail ?? result) };
      const content = text(result?.content ?? item.content ?? item.markdown ?? item.skill_md);
      const upstream = item.source?.kind === 'github' ? item.source : null;
      const detailRows = [['工作区', agents[ws.agent]], ['状态', statusLabel(item)], ['来源', sourceLabel(sourceOf(item))],
        ['GitHub 仓库', upstream?.url], ['分支或标签', upstream?.ref], ['仓库内目录', upstream?.subdir],
        ['自动更新', upstream && item.automatic_update_paused ? '回滚后已暂停，手动应用更新后恢复' : ''],
        ['版本', versionOf(item)], ['库内目录', external ? '' : text(item.path)], ['安装目录', text(item.installed_path)],
        ...list(item.install_conflicts).map(path => ['同名冲突目录', path])];
      showModal(title, `<dl class="skills-meta">${detailRows.filter(([, value]) => value).map(([key, value]) => `<div><dt>${key}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>
      ${item.description ? `<p${descriptionAttr(ws, item)}>${esc(descriptionOf(ws, item))}</p>` : ''}
      ${item.description && translationEnabled(ws) ? `<details class="skills-document" data-skill-original="${esc(originalOf(item))}"${ws.translations.get(originalOf(item)) ? '' : ' hidden'}><summary>简介原文</summary><pre>${esc(item.description)}</pre></details>` : ''}
      ${tagsOf(item).length ? `<div class="skills-tags">${tagsOf(item).map(tag).join('')}</div>` : ''}
      ${content ? `<details class="skills-document" open><summary>SKILL.md</summary><pre>${esc(content)}</pre></details>` : ''}
      ${item.readme_md ? `<details class="skills-document"><summary>README.md</summary><pre>${esc(item.readme_md)}</pre></details>` : ''}
      <div class="skills-actions">${external ? `${btn('导入库', 'adopt', { value: id, disabled: !skill.path })}${btn('查找 GitHub 来源', 'external-github-source', { value: id })}` : `${btn('编辑标签', 'tags', { value: id, glyph: 'tag' })}${btn('版本历史', 'history', { value: id, glyph: 'clock' })}${btn('预览更新', 'update', { value: id })}${btn(upstream ? '更改 GitHub 来源' : '关联 GitHub', 'github-source', { value: id })}${btn('导出', 'export', { value: id, glyph: 'download' })}${btn('删除', 'delete', { value: id, tone: 'danger-quiet' })}`}</div>`);
    });
  }

  function discoverGitHubSource(ws, skill) {
    const id = idOf(skill);
    const title = `GitHub 来源 · ${nameOf(skill)}`;
    const manual = btn('手动指定来源', 'github-source-manual', { value: id });
    return modalRead(ws, title, async stillOpen => {
      let result;
      try {
        result = await request(ws.agent, 'github_discover', { id });
      } catch (error) {
        if (stillOpen()) showModal(title, `${note(error.message, 'alert')}<div class="skills-actions">${btn('重试查找', 'github-source', { value: id })}${manual}</div>`);
        return;
      }
      if (!stillOpen()) return;
      const matches = list(result?.matches);
      const incomplete = result?.incomplete ? note('部分来源暂时无法核对，可重试查找。') : '';
      if (!matches.length || !result?.preview_id) {
        showModal(title, `<p>未找到与本地文件一致的公开 GitHub 版本。</p>${incomplete}<div class="skills-actions">${result?.incomplete ? btn('重试查找', 'github-source', { value: id }) : ''}${manual}</div>`);
        return;
      }
      const rows = matches.map((source, index) => `<label class="skills-check-row"><input type="radio" name="candidate" value="${index}"${matches.length === 1 ? ' checked' : ''} required><span><strong>${esc(text(source.url).replace(/^https:\/\/github\.com\//, ''))}</strong><small>${esc(source.ref || '默认分支')} · ${esc(source.subdir || '仓库根目录')}</small><small>提交 ${esc(text(source.revision).slice(0, 12))}</small></span></label>`).join('');
      showModal(title, `<p>${matches.length === 1 ? '找到一项与本地文件一致的 GitHub 来源。' : `找到 ${matches.length} 项与本地文件一致的 GitHub 来源，请选择要跟踪的仓库。`}</p>${incomplete}
        <div class="skills-check-list" role="radiogroup" aria-label="GitHub 来源">${rows}</div><div class="skills-actions">${manual}</div>`, {
        label: '确认关联', submit: form => {
          const selected = fieldValue(form, 'candidate');
          if (selected === '' || !matches[Number(selected)]) throw new Error('请选择 GitHub 来源。');
          return change(ws, 'github_link_discovered', { id, preview_id: result.preview_id, candidate: Number(selected) }, '已关联 GitHub 来源。');
        }
      });
    }, '正在查找并核对 GitHub 来源…');
  }

  function showGitHubSource(ws, skill, external) {
    const id = idOf(skill);
    const title = `GitHub 来源 · ${nameOf(skill)}`;
    return modalRead(ws, title, async stillOpen => {
      const { source } = await request(ws.agent, 'github_source', { id });
      if (!stillOpen()) return;
      if (external) {
        if (source) showImport(ws, 'github', source);
        else showModal(title, `<p>安装记录中没有可用的 GitHub 来源。</p>${btn('手动从 GitHub 导入', 'import-mode', { value: 'github' })}`);
        return;
      }
      showModal(title, `<p>关联时会核对库内文件与所选 GitHub 版本。</p>
        ${field('GitHub 仓库或技能链接', 'url', { value: source?.url, hint: 'https://github.com/owner/repo', required: true })}
        <div class="skills-form-columns">${field('分支或标签（可选）', 'ref', { value: source?.ref })}${field('子目录（可选）', 'subdir', { value: source?.subdir })}</div>`, {
        label: '核对并关联', submit: form => {
          const payload = { id, url: fieldValue(form, 'url') };
          for (const key of ['ref', 'subdir']) if (fieldValue(form, key)) payload[key] = fieldValue(form, key);
          return change(ws, 'github_link', payload, '已关联 GitHub 来源。');
        }
      });
    });
  }
  function showHistory(ws, skill) {
    const id = idOf(skill);
    const title = `${nameOf(skill)} · 版本历史`;
    modalRead(ws, title, async stillOpen => {
      const payload = await request(ws.agent, 'history', { id });
      if (!stillOpen()) return;
      const versions = list(payload?.versions ?? payload?.history ?? payload);
      showModal(title, `${versions.length ? `<ul class="skills-records">${versions.map((entry, index) => {
        const versionId = text(entry.version_id ?? entry.id ?? entry.revision ?? entry.version);
        const details = [dateLabel(entry.created_at ?? entry.timestamp), text(entry.reason ?? entry.message)].filter(Boolean).join(' · ');
        return `<li><div><strong>${esc(text(entry.version ?? entry.revision) || versionId)}</strong><span>${esc(details)}</span></div>${entry.current === true || (versionOf(skill) && versionOf(entry) === versionOf(skill)) ? tag('当前版本') : btn('回滚', 'rollback', { value: index, disabled: !versionId })}</li>`;
      }).join('')}</ul>` : '<p>暂无版本记录</p>'}<div class="skills-actions">${btn('返回详情', 'detail', { value: id })}</div>`);
      modal.history = { skill, versions };
    });
  }
  async function checkUpdates(ws) {
    if (ws.busy) return;
    ws.busy = '正在检查来源更新…';
    ws.notice = ws.error = '';
    paint(ws);
    try {
      const result = await request(ws.agent, 'check_updates');
      const updates = list(result?.updates ?? result);
      ws.updates = new Map(updates.map(item => [idOf(item), item]));
      ws.notice = `已检查 ${updates.length} 项：${updates.filter(item => item.status === 'available').length} 项可更新，${updates.filter(item => item.status === 'unsupported').length} 项来源不支持更新。${result?.truncated ? '本次未覆盖全部技能。' : ''}`;
      const errors = updates.filter(item => item.status === 'error');
      if (errors.length) ws.error = `检查失败（${errors.length} 项）：${errors.map(item => `${idOf(item)}：${apiMessage(item.error ?? item)}`).join('；')}`;
    } catch (error) { ws.error = error.message; }
    finally { ws.busy = ''; paint(ws); }
  }
  function showUpdate(ws, skill) {
    const id = idOf(skill);
    const title = `${nameOf(skill)} · 更新预览`;
    modalRead(ws, title, async stillOpen => {
      const result = await request(ws.agent, 'update_preview', { id });
      if (!stillOpen()) return;
      const changes = list(result?.changes);
      const statuses = { added: '新增', modified: '修改', deleted: '删除' };
      const body = `<p>${changes.length ? `${changes.length} 个文件有变更` : '没有可应用的更新。'}</p>${result?.local_modified ? note('已检测到本地修改，更新被阻止，现有内容会保留。', 'alert') : ''}${result?.truncated ? note('预览已截断，部分差异未展示。') : ''}<div class="skills-diff">${changes.map(item => `<details class="skills-document"><summary>${esc(statuses[item.status] || item.status)} · ${esc(item.path)}</summary><pre>${esc(text(item.diff) || (item.binary ? '二进制文件' : '未展示差异'))}</pre></details>`).join('')}</div>`;
      showModal(title, body, result?.update_token && !result.local_modified ? { label: '应用更新', submit: async () => {
        await change(ws, 'apply_update', { id, update_token: result.update_token, confirm: true }, '已应用更新。');
        ws.updates.delete(id);
        paint(ws);
      } } : {});
    });
  }

  const activityNames = { install: '安装', disable: '停用', delete: '删除', tags: '编辑标签', preset_save: '保存预设', preset_apply: '启停预设成员', preset_delete: '删除预设', import_selected: '导入技能', apply_update: '应用更新', rollback: '回滚版本', root: '设置目录', export: '导出技能', backup_create: '创建备份', backup_restore: '恢复备份', sync_connect: '设置同步连接', sync_push: '推送同步', sync_pull: '拉取同步', sync_resolve: '处理同步冲突' };
  function showActivity(ws) {
    const title = `${agents[ws.agent]} · 活动日志`;
    return modalRead(ws, title, async stillOpen => {
      const result = await request(ws.agent, 'activity');
      if (!stillOpen()) return;
      const entries = list(result?.entries).filter(item => item.agent === ws.agent);
      showModal(title, `${entries.length ? `<ul class="skills-records">${entries.map(item => `<li><div><strong>${esc(activityNames[item.op] ?? item.op)}</strong><span>${esc(dateLabel(item.time))}</span></div>${tag(item.result === 'ok' ? '成功' : '失败')}</li>`).join('')}</ul>` : '<p>暂无活动日志</p>'}${btn('导出日志', 'activity-export', { glyph: 'download' })}`);
    });
  }
  async function exportActivity(ws) {
    const response = await request(ws.agent, 'activity_export');
    if (typeof response?.content !== 'string') throw new Error('日志导出格式不正确。');
    const url = URL.createObjectURL(new Blob([response.content], { type: 'application/x-ndjson' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `skills-activity-${ws.agent}.jsonl`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    ws.notice = '已发起活动日志下载。';
    if (ws === current()) say(ws.notice);
    paint(ws);
  }

  const conflictOptions = { keep_local: '保留本机并发布到远端', use_remote: '使用远端并替换本机库', keep_both: '保留两份，重命名冲突项并发布到远端' };
  const conflictValid = conflict => /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/i.test(text(conflict?.remote_revision)) &&
    /^[a-f0-9]{64}$/i.test(text(conflict?.local_digest)) && list(conflict?.choices).some(choice => Object.hasOwn(conflictOptions, choice));
  function showConflict(ws, conflict) {
    ws.conflict = conflict;
    paint(ws);
    const rows = [['冲突技能', list(conflict.skill_ids).join('、')], ['冲突预设', list(conflict.preset_names).join('、')], ['共同基线', conflict.baseline_available === true ? '已找到' : conflict.baseline_available === false ? '未找到' : '']];
    showModal(`${agents[ws.agent]} · 处理同步冲突`, `<p>本地与远端均有变更。处理前将分别保存两侧的快照。</p><dl class="skills-meta"><div><dt>远端版本</dt><dd><code>${esc(conflict.remote_revision)}</code></dd></div><div><dt>本地摘要</dt><dd><code>${esc(conflict.local_digest)}</code></dd></div>${rows.filter(([, value]) => value).map(([key, value]) => `<div><dt>${key}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl><div class="skills-check-list">${Object.entries(conflictOptions).filter(([key]) => conflict.choices.includes(key)).map(([key, label]) => `<label class="skills-check-row"><input type="radio" name="strategy" value="${key}" required><span>${esc(label)}</span></label>`).join('')}</div>`, {
      label: '下一步', submit: form => {
        const strategy = fieldValue(form, 'strategy');
        if (!Object.hasOwn(conflictOptions, strategy) || !conflict.choices.includes(strategy)) throw new Error('请选择冲突处理方式。');
        confirmAction('确认处理同步冲突', `${agents[ws.agent]}：${conflictOptions[strategy]}。此操作只应用于刚刚核对的两个版本。`,
          () => runSync(ws, 'sync_resolve', { strategy, expected_remote_revision: conflict.remote_revision, expected_local_digest: conflict.local_digest, confirm: true }), { label: '确认', danger: true });
      }
    });
  }
  async function runSync(ws, op, payload) {
    try {
      const result = await change(ws, op, payload, result => result?.changed === false && op !== 'sync_connect' ? '同步完成，没有需要传输的变更。' : '同步操作完成。', op === 'sync_pull' || op === 'sync_resolve');
      ws.conflict = null;
      if (op === 'sync_resolve') ws.resolvedConflict = `${payload.expected_remote_revision}:${payload.expected_local_digest}`;
      ws.sync = result?.status ?? ws.sync;
      paint(ws);
      await readSide(ws, 'backup_list');
      if (ws.view === 'settings' || ws.autoSync) await readSide(ws, 'autosync_status');
      const merge = result?.merge;
      if (merge && ['auto', 'keep_both'].includes(merge.mode)) {
        if (op === 'sync_push') await loadState(ws);
        const renamed = [...list(merge.renamed_skills).map(item => ['技能', item.from_id, item.to_id]), ...list(merge.renamed_presets).map(item => ['预设', item.from_name, item.to_name])];
        showModal(`${agents[ws.agent]} · 合并结果`, `<p>${merge.mode === 'auto' ? '已自动合并。' : '已保留两份。'}${merge.published ? '已发布到远端。' : '结果保存在本机，未发布到远端。'}</p>${renamed.length ? `<ul class="skills-records">${renamed.map(([kind, from, to]) => `<li><div><strong>${kind}：${esc(from)}</strong><span>新名称：${esc(to)}</span></div></li>`).join('')}</ul>` : ''}`);
      }
    } catch (error) {
      const isConflict = error.code === 'sync_conflict' || error.code === 'sync_conflict_stale';
      if (isConflict && error.status?.state === 'conflict') ws.sync = { ...ws.sync, ...error.status };
      if (isConflict && conflictValid(error.conflict)) {
        showConflict(ws, error.conflict);
        if (error.code === 'sync_conflict_stale') modalError(error);
        return;
      }
      if (op === 'sync_resolve') {
        ws.conflict = null;
        refreshView(ws);
      }
      throw error;
    }
  }

  function startGithub(ws) {
    const title = `${agents[ws.agent]} · GitHub 备份仓库`;
    return modalRead(ws, title, async stillOpen => {
      const status = await request(ws.agent, 'github_status');
      if (!stillOpen()) return;
      const login = text(status?.login);
      if (!status?.available || !status?.authenticated || !login) {
        const guidance = status?.guidance;
        showModal(title, `<p>${status?.available ? 'GitHub CLI 尚未登录。' : '本机未找到 GitHub CLI。'}</p><p>可在终端登录后重新检查，或用本机 SSH 密钥连接已有私有仓库。</p><code>${esc(guidance?.login_command || 'gh auth login --hostname github.com')}</code><div class="skills-actions">${btn('重新检查登录', 'github-connect')}${btn('手动连接 SSH', 'sync-connect')}</div>`);
        return;
      }
      githubRepository(ws, title, login, 'select');
    });
  }
  function githubRepository(ws, title, login, mode) {
    const create = mode === 'create';
    showModal(title, `<p>已登录：<strong>${esc(login)}</strong>。请选择受信任、专用于备份的私有仓库。</p><div class="skills-segment" role="group" aria-label="仓库操作"><button type="button" data-skill-action="github-mode" data-value="select" aria-pressed="${!create}">使用已有仓库</button><button type="button" data-skill-action="github-mode" data-value="create" aria-pressed="${create}">新建私有仓库</button></div>${field('仓库（OWNER/REPO）', 'repo', { required: true, value: create ? `${login}/${ws.agent}-skills-backup` : '' })}<label class="skills-field"><span>连接协议</span><select name="protocol"><option value="https">HTTPS</option><option value="ssh">SSH</option></select></label>`, { label: '核对连接', submit: form => {
      const repo = fieldValue(form, 'repo');
      const protocol = fieldValue(form, 'protocol');
      if (!/^[\w.-]+\/[\w.-]+$/.test(repo) || !['https', 'ssh'].includes(protocol)) throw new Error('请输入 OWNER/REPO 格式的 GitHub 仓库名。');
      confirmAction('确认 GitHub 连接', `${create ? '新建私有仓库' : '连接已有私有仓库'} ${repo}（${protocol.toUpperCase()}），用于 ${agents[ws.agent]} 技能备份。此操作不会推送技能文件。`, async () => {
        const result = await request(ws.agent, 'github_connect', { mode, repo, protocol, confirm: true, login });
        const remote_url = text(result?.remote);
        if (!validRemote(remote_url)) throw new Error('GitHub 服务未返回有效的仓库地址。');
        const payload = { remote_url, confirm: true };
        if (result?.credential_helper === 'gh') payload.credential_helper = 'gh';
        try { await runSync(ws, 'sync_connect', payload); }
        catch (error) {
          if (!result?.created) throw error;
          showModal('仓库已创建，连接未保存', `<p>${esc(repo)} 已创建。可以使用返回的地址重新连接，无需再次创建仓库。</p><code>${esc(remote_url)}</code>${note(error.message, 'alert')}`, { label: '重试保存连接', submit: () => runSync(ws, 'sync_connect', payload) });
        }
      }, { label: create ? '确认创建并连接' : '确认连接' });
    } });
    modal.github = { title, login };
  }

  function routeAction(ws, action, value, control) {
    if (action === 'close') { if (!modal?.busy) dialog.close(); return; }
    if (modal?.busy) return;
    if (action === 'agent') {
      if (!Object.hasOwn(agents, value) || value === active) return;
      ws.selection.clear();
      active = value;
      const target = current();
      render();
      loadState(target);
      refreshView(target);
      say(`已切换到 ${agents[active]} 全局工作区。`);
      return;
    }
    if (action === 'view') {
      if (!Object.hasOwn(views, value) || ws.view === value) return;
      ws.view = value;
      ws.selection.clear();
      ws.query = ws.source = ws.tag = '';
      ws.filter = 'all';
      render();
      refreshView(ws);
      return;
    }
    if (action === 'refresh') { loadState(ws); refreshView(ws); return; }
    if (action === 'clear-filters') {
      ws.query = ws.source = ws.tag = '';
      ws.filter = 'all';
      render();
      host.querySelector('#skillsSearch')?.focus();
      return;
    }
    if (action === 'activity') return showActivity(ws);
    if (action === 'activity-export') return exportActivity(ws);
    if (action === 'read-root') return readSide(ws, 'root');
    if (action === 'autosync-refresh') { refreshView(ws); return; }
    if (action === 'autosync-conflict' && conflictValid(ws.autoSync?.conflict)) return showConflict(ws, ws.autoSync.conflict);
    if (action === 'autosync-resume' && ws.autoSync?.enabled && ws.autoSync?.paused) {
      return confirmAction('恢复自动同步', `${agents[ws.agent]} 将恢复与当前仓库的自动同步。`, () => changeAutoSync(ws, 'autosync_resume', { confirm: true }), { label: '确认恢复' });
    }
    if (ws.busy) return;
    if (ws.view === 'library' && action.startsWith('batch-')) {
      const items = getResults(ws).items;
      if (action === 'batch-clear' || action === 'batch-select') {
        if (action === 'batch-clear' || items.every(item => ws.selection.has(idOf(item)))) ws.selection.clear();
        else ws.selection = new Set(items.map(idOf));
        paintSelection(ws);
        return;
      }
      const op = action.slice(6);
      if (!['install', 'disable', 'delete'].includes(op)) return;
      const selected = selectedItems(ws, items).filter(item => op === 'delete' || (op === 'disable' ? canDisable(item) : canInstall(item)));
      if (!selected.length) return;
      const verb = { install: '安装', disable: '停用', delete: '删除' }[op];
      const title = `批量${verb} ${selected.length} 项技能`;
      const ids = selected.map(idOf);
      return showModal(title, `<p>工作区：${agents[ws.agent]}${op === 'delete' ? '。此操作也会移除受管安装和预设成员资格。' : ''}</p><ul class="skills-batch-names">${selected.map(item => `<li>${esc(nameOf(item))}</li>`).join('')}</ul>`, { label: `确认${verb}`, danger: op === 'delete', submit: async () => {
        await change(ws, op, { ids }, `已${verb} ${ids.length} 项技能。`);
        ids.forEach(id => ws.selection.delete(id));
        paint(ws);
      } });
    }
    if (action === 'import') return showImport(ws);
    if (action === 'import-mode' && Object.hasOwn(importModes, value)) return showImport(ws, value);
    if (action === 'select-all') {
      const checks = [...dialog.querySelectorAll('input[name="candidate"]')];
      const select = checks.some(input => !input.checked);
      checks.forEach(input => { input.checked = select; });
      control.querySelector('span').textContent = select ? '取消全选' : '全选';
      return;
    }
    if (action === 'market-import') {
      const item = ws.market.items?.[Number(value)];
      if (!item?.url) return;
      return modalRead(ws, `预览 ${nameOf(item)}`, async stillOpen => {
        if (!stillOpen()) return;
        const payload = { url: item.url };
        if (item.skill_id) payload.skill_id = item.skill_id;
        await showCandidates(ws, 'github', payload, stillOpen);
      });
    }
    if (action === 'check-updates') return checkUpdates(ws);
    if (action === 'github-connect') return startGithub(ws);
    if (action === 'github-mode' && modal?.github && ['select', 'create'].includes(value)) {
      return githubRepository(ws, modal.github.title, modal.github.login, value);
    }
    if (action === 'edit-root' || action === 'reset-root') {
      const setRoot = async path => {
        const result = await change(ws, 'root', { path }, '已保存全局技能目录。');
        ws.rootInfo = result?.root;
        paint(ws);
      };
      if (action === 'reset-root') return confirmAction('恢复默认技能目录', `将 ${agents[ws.agent]} 的安装目录恢复为默认值，不迁移现有文件。`, () => setRoot(null), { label: '恢复默认' });
      return showModal(`更改 ${agents[ws.agent]} 技能目录`, `${field('新目录（本机绝对路径）', 'path', { value: rootPath(ws.rootInfo ?? ws.data?.root), required: true })}<p>更改目录不会迁移现有文件。仍有受管技能部署时，请先停用。</p>`, { label: '确认更改', submit: form => setRoot(fieldValue(form, 'path')) });
    }
    if (action === 'preset-save') {
      const skillList = list(ws.data?.skills).filter(item => !isExternal(item));
      return showModal(`新建 ${agents[ws.agent]} 预设`, `${field('预设名称', 'name', { required: true, maxlength: 80 })}<div class="skills-check-list">${skillList.map(item => `<label class="skills-check-row"><input type="checkbox" name="member" value="${esc(idOf(item))}" ${isInstalled(item) ? 'checked' : ''}><span>${esc(nameOf(item))}</span></label>`).join('')}</div>`, { label: '保存预设', submit: form => {
        const name = fieldValue(form, 'name');
        if (!name) throw new Error('请输入预设名称。');
        if (ws.data.presets.some(item => presetName(item) === name)) throw new Error('该名称已存在。请使用不同的名称。');
        return change(ws, 'preset_save', { name, ids: form.getAll('member') }, '已保存预设。');
      } });
    }
    if (action === 'preset-apply') {
      const name = ws.preset;
      if (!name || !ws.data.presets.some(item => presetName(item) === name) || !['enable', 'disable'].includes(value)) return;
      const enabled = value === 'enable';
      const verb = enabled ? '启用' : '停用';
      return confirmAction(`${verb}预设成员`, `${verb} ${agents[ws.agent]} 预设「${name}」中的技能。`, () => change(ws, 'preset_apply', { name, enabled }, `已${verb}预设成员。`), { label: `${verb}成员` });
    }
    if (action === 'preset-delete') {
      const name = ws.preset;
      if (!name || !ws.data.presets.some(item => presetName(item) === name)) return;
      return confirmAction('删除预设', `删除 ${agents[ws.agent]} 预设「${name}」，保留其成员现有的安装状态。`, async () => {
        await change(ws, 'preset_delete', { name }, '已删除预设。');
        ws.preset = '';
        paint(ws);
      }, { label: '删除预设', danger: true });
    }
    if (action === 'backup-create') {
      return confirmAction('创建本地备份', `为 ${agents[ws.agent]} 工作区创建备份。`, async () => {
        await change(ws, 'backup_create', {}, '已创建本地备份。', false);
        await readSide(ws, 'backup_list');
      }, { label: '创建备份' });
    }
    if (action === 'backup-restore' && ws.backups?.some(item => item.backup_id === value)) {
      return confirmAction('恢复本地备份', `将 ${agents[ws.agent]} 的受管技能库恢复到备份 ${value}。恢复前会创建保护备份。`, async () => {
        await change(ws, 'backup_restore', { backup_id: value, confirm: true }, '已恢复备份。');
        await readSide(ws, 'backup_list');
        await readSide(ws, 'sync_status');
      }, { label: '确认恢复', danger: true });
    }
    if (action === 'sync-connect') {
      return showModal(`连接 ${agents[ws.agent]} 同步仓库`, `${field('Git 仓库地址（HTTPS 或 SSH，不含凭据）', 'remote', { value: ws.sync?.remote_url, required: true })}<p>私有仓库请使用本机已配置的 SSH 密钥；HTTPS 用于无需应用登录的远端。</p>`, {
        label: '确认连接', submit: form => {
          const remote_url = fieldValue(form, 'remote');
          if (!validRemote(remote_url)) throw new Error('请输入不含凭据的 HTTPS 或 SSH Git 地址。');
          return runSync(ws, 'sync_connect', { remote_url, confirm: true });
        }
      });
    }
    if (action === 'sync-push' || action === 'sync-pull') {
      const push = action === 'sync-push';
      return confirmAction(push ? '推送到远端' : '从远端拉取', push ? `将 ${agents[ws.agent]} 的受管技能库发送到 ${ws.sync.remote_url}。` : `从 ${ws.sync.remote_url} 拉取 ${agents[ws.agent]} 的技能库。`, () => runSync(ws, push ? 'sync_push' : 'sync_pull', { confirm: true }), { label: push ? '确认推送' : '确认拉取' });
    }
    if (action === 'sync-conflict' && conflictValid(ws.conflict)) return showConflict(ws, ws.conflict);
    if (action === 'rollback' && modal.history) {
      const { skill, versions } = modal.history;
      const version = versions[Number(value)]?.version;
      if (!version) return;
      return confirmAction('回滚技能版本', `将 ${agents[ws.agent]} 中的「${nameOf(skill)}」回滚到版本 ${version}。`, () => change(ws, 'rollback', { id: idOf(skill), version }, '已回滚版本。'), { label: '确认回滚', danger: true });
    }
    const external = ['external-detail', 'adopt', 'external-github-source'].includes(action);
    const skill = selectedSkill(ws, value, external);
    if (!skill) return;
    const id = idOf(skill);
    const name = nameOf(skill);
    if (action === 'detail' || action === 'external-detail') return showDetail(ws, skill, external);
    if (action === 'github-source' && skill.source?.kind !== 'github') return discoverGitHubSource(ws, skill);
    if (['github-source', 'github-source-manual', 'external-github-source'].includes(action)) return showGitHubSource(ws, skill, external);
    if (action === 'adopt') return showImport(ws, 'local', { path: skill.path });
    if (action === 'update') return showUpdate(ws, skill);
    if (action === 'history') return showHistory(ws, skill);
    if (action === 'install' && canInstall(skill)) return showModal(`安装「${name}」`, `<p>安装到 ${agents[ws.agent]} 的全局技能目录。</p><label class="skills-field"><span>部署方式</span><select name="mode"><option value="copy">复制文件</option><option value="link">符号链接</option></select></label>`, { label: '确认安装', submit: form => change(ws, 'install', { id, mode: form.get('mode') }, '已安装技能。') });
    if (action === 'disable' && canDisable(skill)) return confirmAction('停用技能', `从 ${agents[ws.agent]} 的安装目录移除「${name}」，技能库中的副本仍保留。`, () => change(ws, 'disable', { id }, '已停用技能。'), { label: '确认停用' });
    if (action === 'delete') return confirmAction('删除技能', `从 ${agents[ws.agent]} 的技能库删除「${name}」，并移除它的受管安装和预设成员资格。`, () => change(ws, 'delete', { id }, '已删除技能。'), { label: '确认删除', danger: true });
    if (action === 'tags') return showModal(`编辑标签 · ${name}`, field('标签（用逗号分隔）', 'tags', { value: tagsOf(skill).join(', ') }), { label: '保存标签', submit: form => change(ws, 'tags', { id, tags: [...new Set(fieldValue(form, 'tags').split(/[,，]/).map(value => value.trim()).filter(Boolean))] }, '已保存标签。') });
    if (action === 'export') return showModal(`导出「${name}」`, field('服务主机上的绝对路径（.zip 或 .skill）', 'path', { required: true }), { label: '导出技能', submit: form => change(ws, 'export', { id, path: fieldValue(form, 'path') }, result => `已导出到 ${text(result?.path) || fieldValue(form, 'path')}。`, false) });
  }

  async function changeAutoSync(ws, op, payload) {
    const result = await request(ws.agent, op, payload);
    if (!result?.status || result.status.agent && result.status.agent !== ws.agent) throw new Error('自动同步状态格式不正确。');
    ws.autoSync = result.status;
    ws.notice = payload.enabled === false ? '已关闭自动同步。' : op === 'autosync_resume' ? '已恢复自动同步。' : '已开启自动同步。';
    ws.error = '';
    delete ws.readErrors.autosync_status;
    paint(ws);
  }

  function bind() {
    const root = document.getElementById('view-skills');
    root.addEventListener('click', event => {
      const control = event.target.closest('[data-skill-action]');
      if (!control || control.disabled) return;
      const ws = current();
      Promise.resolve().then(() => routeAction(ws, control.dataset.skillAction, control.dataset.value, control)).catch(error => {
        if (dialog.open && modal?.ws === ws) modalError(error);
        else { ws.error = error.message; paint(ws); }
      });
    });
    host.addEventListener('input', event => {
      const ws = current();
      if (event.target.id === 'skillsSearch') {
        ws.query = event.target.value;
        host.querySelector('#skillsResults').innerHTML = renderResults(ws);
        void translateVisible(ws);
        say(`当前筛选匹配 ${getResults(ws).items.length} 项技能。`);
      }
    });
    host.addEventListener('change', event => {
      const ws = current();
      const automation = { skillsTranslate: 'translate', skillsGitHubAuto: 'github_auto' }[event.target.id];
      if (automation) {
        const input = event.target;
        const enabled = input.checked;
        input.checked = ws.automation?.settings?.[automation] === true;
        const name = automation === 'translate' ? '自动翻译' : 'GitHub 自动更新';
        const apply = async () => {
          const result = await change(ws, 'skill_automation', { [automation]: enabled }, `已${enabled ? '开启' : '关闭'}${name}。`, false);
          ws.automation = result;
          if (ws.data) ws.data.automation = result.settings;
          ws.translationRetry = 0;
          ws.translationError = '';
          paint(ws);
        };
        if (enabled) {
          const message = automation === 'translate' ? '看板会把待翻译的技能简介发给 DeepL，按你的 API 套餐计算用量。'
            : '看板会从已关联的公开仓库拉取新版本并更新未修改的受管技能。每次更新保留版本记录。';
          confirmAction(`开启${name}`, message, apply, { label: '确认开启' });
        } else {
          input.disabled = true;
          apply().catch(error => { ws.error = error.message; paint(ws); });
        }
        return;
      }
      if (event.target.id === 'skillsAutoSync') {
        const input = event.target;
        const enabled = input.checked;
        input.checked = ws.autoSync?.enabled === true;
        if (enabled && syncConnected(ws) === true && ws.autoSync?.state !== 'stopping') {
          confirmAction('开启自动同步', `允许 ${agents[ws.agent]} 自动拉取和推送当前受管技能库。远端：${ws.sync?.remote_url || '已连接的仓库'}。`, () => changeAutoSync(ws, 'autosync_set', { enabled: true, confirm: true }), { label: '确认开启' });
        } else if (!enabled && ws.autoSync?.enabled) {
          input.disabled = true;
          ws.busy = '正在关闭自动同步…';
          changeAutoSync(ws, 'autosync_set', { enabled: false }).catch(error => { ws.error = error.message; }).finally(() => {
            ws.busy = '';
            paint(ws);
            if (ws === current()) host.querySelector('#skillsAutoSync:not(:disabled)')?.focus({ preventScroll: true });
          });
        }
        return;
      }
      const selected = event.target.dataset.skillSelection;
      if (selected && ws.view === 'library' && !ws.busy) {
        if (event.target.checked) ws.selection.add(selected);
        else ws.selection.delete(selected);
        paintSelection(ws);
        return;
      }
      const key = { skillsSource: 'source', skillsTag: 'tag', skillsFilter: 'filter', skillsPreset: 'preset' }[event.target.id];
      if (key) { ws[key] = event.target.value; render(); }
      else if (event.target.id === 'skillsMarketBoard') {
        ws.market.query = event.target.form.elements.query.value.trim();
        ws.market.board = event.target.value;
        searchMarket(ws);
      }
    });
    host.addEventListener('submit', event => {
      if (event.target.dataset.skillForm !== 'market') return;
      event.preventDefault();
      const ws = current();
      const form = new FormData(event.target);
      ws.market.query = fieldValue(form, 'query');
      ws.market.board = fieldValue(form, 'board');
      searchMarket(ws);
    });
    dialog.addEventListener('cancel', event => { if (modal?.busy) event.preventDefault(); });
    dialog.addEventListener('close', () => {
      if (dialog.open) return;
      modal = null;
      const preferred = modalReturn?.isConnected && !modalReturn.disabled ? modalReturn : focusKey(modalReturn);
      (preferred || host.querySelector('[aria-current="page"]'))?.focus({ preventScroll: true });
      modalReturn = null;
    });
    dialog.addEventListener('submit', async event => {
      event.preventDefault();
      if (!modal?.submit || modal.busy) return;
      const ticket = modal;
      const form = new FormData(event.target);
      const controls = [...event.target.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')];
      ticket.busy = true;
      dialog.setAttribute('aria-busy', 'true');
      dialog.querySelector('[data-dialog-error]').hidden = true;
      controls.forEach(control => { control.disabled = true; });
      const submit = event.submitter;
      const label = submit?.textContent;
      if (submit) submit.textContent = '处理中…';
      try {
        await ticket.submit(form);
        if (modal === ticket && dialog.open) dialog.close();
      } catch (error) {
        if (modal === ticket && dialog.open) modalError(error);
      } finally {
        ticket.busy = false;
        if (dialog.open && modal === ticket) {
          controls.forEach(control => { control.disabled = false; });
          if (submit) { submit.textContent = label; submit.focus(); }
        }
        if (!modal?.busy) dialog.removeAttribute('aria-busy');
      }
    });
  }

  function open() {
    if (opened) return;
    host = document.getElementById('skillsWorkspace');
    if (!host) return;
    opened = true;
    dialog = document.createElement('dialog');
    dialog.className = 'skills-dialog';
    dialog.setAttribute('aria-labelledby', 'skillsDialogTitle');
    host.after(dialog);
    bind();
    loadState(current());
    setInterval(() => {
      const ws = current();
      if (host.closest('[hidden]') || document.hidden || dialog.open || ws.busy) return;
      if (ws.view === 'settings') readSide(ws, 'autosync_status', true);
      if (ws.view === 'settings' || ws.data?.automation?.github_auto) void readAutomation(ws, true);
      void translateVisible(ws);
    }, 8000);
  }

  window.DashboardSkills = Object.freeze({ open });
})();
