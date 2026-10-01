(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const nf = new Intl.NumberFormat('zh-CN');
  const bridgeMethods = ['get_data_settings', 'preview_data_cleanup', 'clean_history', 'choose_data_directory', 'set_data_directory'];
  const dialog = $('#dataSettingsDialog');
  const openButton = $('#dataSettingsButton');
  const directoryInput = $('#dataDirectoryInput');
  const daysInput = $('#cleanupDaysInput');
  const status = $('#dataSettingsStatus');

  const state = {
    busy: false,
    settings: null,
    preview: null,
    returnFocus: null,
    cleanupDaysTouched: false
  };

  function bridge() {
    const api = window.pywebview && window.pywebview.api;
    return api && bridgeMethods.every(name => typeof api[name] === 'function') ? api : null;
  }

  function messageOf(error, fallback) {
    return error && error.message ? String(error.message) : fallback;
  }

  function setStatus(message = '', isError = false) {
    status.textContent = message;
    status.classList.toggle('is-error', isError);
  }

  function setBusy(busy) {
    state.busy = Boolean(busy);
    dialog.querySelectorAll('button, input').forEach(control => {
      control.disabled = state.busy && !['dataSettingsCloseIcon', 'dataSettingsCloseButton'].includes(control.id);
    });
  }

  function validDays({ showError = true } = {}) {
    const raw = daysInput.value.trim();
    const value = raw ? Number(raw) : NaN;
    const valid = Number.isInteger(value) && value >= 1 && value <= 36500;
    daysInput.setAttribute('aria-invalid', valid ? 'false' : 'true');
    if (!valid && showError) setStatus('请输入 1 到 36500 之间的整数天数。', true);
    return valid ? value : null;
  }

  function isAbsolutePath(value) {
    return /^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+|\/)/.test(value);
  }

  function bytesLabel(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return '—';
    if (bytes < 1024) return `${nf.format(bytes)} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let size = bytes / 1024;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(size)} ${units[unit]}`;
  }

  function currentDirectory() {
    const value = state.settings && state.settings.directory;
    return typeof value === 'string' && value.trim() ? value : '';
  }

  function applySettings(settings, { adoptCleanupDays = false } = {}) {
    if (!settings || typeof settings !== 'object') return;
    state.settings = { ...(state.settings || {}), ...settings };
    const directory = typeof settings.directory === 'string' ? settings.directory.trim() : '';
    if (directory) {
      $('#dataCurrentDirectory').textContent = directory;
      directoryInput.value = directory;
    }

    const databasePath = typeof settings.database_path === 'string' && settings.database_path.trim() ? settings.database_path : '—';
    $('#dataDatabasePath').textContent = databasePath;
    $('#dataDatabaseSize').textContent = bytesLabel(settings.database_bytes);
    const requestCount = Number(settings.request_count);
    $('#dataRequestCount').textContent = Number.isFinite(requestCount) && requestCount >= 0 ? nf.format(requestCount) : '—';
    const earliest = typeof settings.earliest_day === 'string' && settings.earliest_day ? settings.earliest_day : '';
    const latest = typeof settings.latest_day === 'string' && settings.latest_day ? settings.latest_day : '';
    $('#dataHistoryRange').textContent = earliest && latest ? `${earliest} 至 ${latest}` : '暂无记录';

    const savedDays = Number(settings.cleanup_days);
    if (adoptCleanupDays && Number.isInteger(savedDays) && savedDays >= 1 && savedDays <= 36500) {
      daysInput.value = String(savedDays);
      state.cleanupDaysTouched = false;
    }
  }

  function clearPreview() {
    state.preview = null;
    const preview = $('#dataCleanupPreview');
    preview.hidden = true;
    $('#cleanupPreviewSummary').textContent = '';
    $('#cleanupRequestCount').textContent = '0';
    $('#cleanupSnapshotCount').textContent = '0';
    $('#cleanupPreviewEmpty').hidden = true;
    $('#dataCleanupConfirm').hidden = true;
  }

  function refreshDashboard() {
    document.dispatchEvent(new CustomEvent('data-settings:refresh'));
  }

  async function readSettings() {
    const api = bridge();
    if (!api) return;
    setBusy(true);
    setStatus('正在读取数据设置…');
    try {
      const result = await api.get_data_settings();
      if (!result || !result.ok || !result.settings || typeof result.settings !== 'object') {
        setStatus(result && result.error ? String(result.error) : '无法读取数据设置；当前保存位置未更改。', true);
        return;
      }
      applySettings(result.settings, { adoptCleanupDays: !state.cleanupDaysTouched });
      setStatus('');
    } catch (error) {
      setStatus(`无法读取数据设置；当前保存位置未更改。${messageOf(error, '') ? ` ${messageOf(error, '')}` : ''}`, true);
    } finally {
      setBusy(false);
    }
  }

  async function openDialog() {
    if (!bridge() || dialog.open || state.busy) return;
    state.returnFocus = document.activeElement;
    clearPreview();
    setStatus('');
    dialog.showModal();
    await readSettings();
  }

  async function chooseDirectory() {
    const api = bridge();
    if (!api || state.busy) return;
    const previousInput = directoryInput.value;
    setBusy(true);
    setStatus('正在打开文件夹选择器…');
    try {
      const result = await api.choose_data_directory();
      if (result && result.cancelled) {
        directoryInput.value = previousInput;
        setStatus('已取消选择，当前保存位置未更改。');
      } else if (!result || !result.ok) {
        directoryInput.value = previousInput;
        setStatus(result && result.error ? String(result.error) : '无法选择文件夹；当前保存位置未更改。', true);
      } else if (typeof result.path !== 'string' || !result.path.trim()) {
        directoryInput.value = previousInput;
        setStatus('未收到有效文件夹路径；当前保存位置未更改。', true);
      } else {
        directoryInput.value = result.path;
        directoryInput.setAttribute('aria-invalid', 'false');
        setStatus('已选择新目录；点击“保存并迁移”后才会更改保存位置。');
      }
    } catch (error) {
      directoryInput.value = previousInput;
      setStatus(`无法选择文件夹；当前保存位置未更改。${messageOf(error, '') ? ` ${messageOf(error, '')}` : ''}`, true);
    } finally {
      setBusy(false);
    }
  }

  async function saveDirectory() {
    const api = bridge();
    if (!api || state.busy) return;
    const directory = directoryInput.value.trim();
    if (!directory || !isAbsolutePath(directory)) {
      directoryInput.setAttribute('aria-invalid', 'true');
      setStatus('请输入完整的目录路径，或点击“选择文件夹”。', true);
      directoryInput.focus();
      return;
    }
    directoryInput.setAttribute('aria-invalid', 'false');
    if (directory === currentDirectory()) {
      setStatus('保存位置没有变化。');
      return;
    }

    const oldDirectory = currentDirectory();
    setBusy(true);
    setStatus('正在迁移历史数据…');
    try {
      const result = await api.set_data_directory(directory);
      if (!result || !result.ok) {
        if (oldDirectory) directoryInput.value = oldDirectory;
        setStatus(result && result.error ? String(result.error) : '迁移失败；当前保存位置未更改。', true);
        return;
      }
      const responseSettings = result.settings && typeof result.settings === 'object' ? result.settings : {};
      const responseDirectory = typeof responseSettings.directory === 'string' && responseSettings.directory.trim() ? responseSettings.directory : directory;
      applySettings({ ...(state.settings || {}), ...responseSettings, directory: responseDirectory }, { adoptCleanupDays: false });
      const savedDirectory = currentDirectory() || directory;
      directoryInput.value = savedDirectory;
      const previousPath = typeof result.previous_path === 'string' && result.previous_path.trim() ? `\n迁移前位置：${result.previous_path}` : '';
      setStatus(`保存位置已迁移到：${savedDirectory}\n历史数据已跟随迁移，旧目录备份已保留，可用于恢复。${previousPath}`);
      refreshDashboard();
    } catch (error) {
      if (oldDirectory) directoryInput.value = oldDirectory;
      setStatus(`迁移失败；当前保存位置未更改。${messageOf(error, '') ? ` ${messageOf(error, '')}` : ''}`, true);
    } finally {
      setBusy(false);
    }
  }

  async function previewCleanup() {
    const api = bridge();
    if (!api || state.busy) return;
    const days = validDays();
    if (days === null) return;
    clearPreview();
    setBusy(true);
    setStatus('正在预览清理范围…');
    try {
      const result = await api.preview_data_cleanup(days);
      const preview = result && result.preview;
      const requestCount = Number(preview && preview.request_count);
      const snapshotCount = Number(preview && preview.snapshot_count);
      if (!result || !result.ok) {
        setStatus(result && result.error ? String(result.error) : '无法预览清理范围，请重试。', true);
        return;
      }
      if (!preview || typeof preview.cutoff_day !== 'string' || !preview.cutoff_day.trim() || !Number.isFinite(requestCount) || requestCount < 0 || !Number.isFinite(snapshotCount) || snapshotCount < 0) {
        setStatus('清理预览未返回有效的截止日期或记录数量。', true);
        return;
      }
      state.preview = { days, cutoff_day: preview.cutoff_day, request_count: requestCount, snapshot_count: snapshotCount };
      $('#cleanupPreviewSummary').textContent = `严格早于 ${preview.cutoff_day} 的记录（截止日期当天保留）`;
      $('#cleanupRequestCount').textContent = nf.format(requestCount);
      $('#cleanupSnapshotCount').textContent = nf.format(snapshotCount);
      $('#dataCleanupPreview').hidden = false;
      const empty = requestCount === 0 && snapshotCount === 0;
      $('#cleanupPreviewEmpty').hidden = !empty;
      $('#dataCleanupConfirm').hidden = empty;
      setStatus(empty ? `预览完成：${preview.cutoff_day} 前没有记录，无需清理。` : '预览完成；请核对截止日期和记录数量，再确认清理。');
    } catch (error) {
      setStatus(`预览清理失败。${messageOf(error, '') ? ` ${messageOf(error, '')}` : ''}`, true);
    } finally {
      setBusy(false);
    }
  }

  async function confirmCleanup() {
    const api = bridge();
    if (!api || state.busy) return;
    if (!state.preview) {
      setStatus('请先预览清理范围。', true);
      return;
    }
    const days = validDays();
    if (days === null) return;
    if (days !== state.preview.days) {
      clearPreview();
      setStatus('天数已变化，请重新预览清理范围。', true);
      return;
    }

    const preview = { ...state.preview };
    setBusy(true);
    setStatus('正在清理历史数据并创建备份…');
    try {
      const result = await api.clean_history(preview.days, preview.cutoff_day);
      if (!result || !result.ok) {
        setStatus(result && result.error ? String(result.error) : '清理失败，历史数据未更改。', true);
        return;
      }
      if (result.settings && typeof result.settings === 'object') applySettings(result.settings, { adoptCleanupDays: false });
      state.cleanupDaysTouched = false;
      daysInput.value = String(preview.days);
      const deletedRequests = Number(result.deleted_requests);
      const deletedSnapshots = Number(result.deleted_snapshots);
      const requests = Number.isFinite(deletedRequests) && deletedRequests >= 0 ? deletedRequests : preview.request_count;
      const snapshots = Number.isFinite(deletedSnapshots) && deletedSnapshots >= 0 ? deletedSnapshots : preview.snapshot_count;
      const backupPath = typeof result.backup_path === 'string' && result.backup_path.trim() ? result.backup_path : '备份路径未提供';
      const spaceNote = result.space_reclaimed === false ? '\n历史已清理，空间回收暂未完成。' : '';
      clearPreview();
      setStatus(`已清理严格早于 ${preview.cutoff_day} 的数据：删除请求 ${nf.format(requests)} 条、相关历史记录 ${nf.format(snapshots)} 条。\n备份路径：${backupPath}\n这次截止日期前的数据不会从原日志再次导入；备份可恢复。${spaceNote}`);
      refreshDashboard();
    } catch (error) {
      setStatus(`清理失败。${messageOf(error, '') ? ` ${messageOf(error, '')}` : ''}`, true);
    } finally {
      setBusy(false);
    }
  }

  function bindEvents() {
    openButton.addEventListener('click', openDialog);
    $('#dataSettingsCloseIcon').addEventListener('click', () => dialog.close());
    $('#dataSettingsCloseButton').addEventListener('click', () => dialog.close());
    $('#chooseDataDirectoryButton').addEventListener('click', chooseDirectory);
    $('#saveDataDirectoryButton').addEventListener('click', saveDirectory);
    $('#previewCleanupButton').addEventListener('click', previewCleanup);
    $('#confirmCleanupButton').addEventListener('click', confirmCleanup);
    $('#cancelCleanupButton').addEventListener('click', () => {
      if (state.busy) return;
      clearPreview();
      setStatus('已取消，历史数据未更改。');
    });
    directoryInput.addEventListener('input', () => {
      directoryInput.setAttribute('aria-invalid', 'false');
      setStatus('');
    });
    daysInput.addEventListener('input', () => {
      state.cleanupDaysTouched = true;
      daysInput.setAttribute('aria-invalid', 'false');
      clearPreview();
      setStatus('');
    });
    dialog.addEventListener('close', () => {
      clearPreview();
      setStatus('');
      if (state.returnFocus && state.returnFocus.isConnected) state.returnFocus.focus();
      state.returnFocus = null;
    });
  }

  function initialize() {
    const api = bridge();
    if (api) openButton.hidden = false;
  }

  bindEvents();
  window.addEventListener('pywebviewready', initialize);
  initialize();
})();
