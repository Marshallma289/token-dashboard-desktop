"""Anonymous GitHub release updates, staged before the desktop app exits."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile

from update_helpers import WINDOWS_HELPER, MACOS_HELPER

REPOSITORY = 'Marshallma289/token-dashboard-desktop'
API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_ARCHIVE = 250 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


def resource_dir() -> Path:
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def installation_dir() -> Path:
    executable = Path(sys.executable).resolve()
    if sys.platform == 'darwin':
        return next((p for p in executable.parents if p.suffix == '.app'), executable.parent)
    return executable.parent


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('无效的更新信息')
    return value


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def version_tuple(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('无效的版本号')
    return tuple(int(part) for part in value.split('.'))


def safe_asset_url(url: str) -> bool:
    return url.startswith(f'https://github.com/{REPOSITORY}/releases/download/') and not urlparse(url).username


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlparse(newurl).hostname or ''
        if urlparse(newurl).scheme != 'https' or not (host == 'github.com' or host.endswith('.githubusercontent.com')):
            raise ValueError('更新下载被重定向到不受支持的地址')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url: str, limit: int, *, progress=None, target: Path | None = None) -> bytes:
    if url != API and not safe_asset_url(url):
        raise ValueError('更新文件不属于指定仓库')
    request = Request(url, headers={'User-Agent': 'CodexTokenDashboard-Updater', 'Accept': 'application/vnd.github+json' if url == API else 'application/octet-stream'})
    received = 0
    chunks = []
    with build_opener(GitHubRedirects).open(request, timeout=30) as response:
        size = int(response.headers.get('Content-Length') or 0)
        if size > limit:
            raise ValueError('更新文件超过大小限制')
        output = target.open('wb') if target else None
        try:
            while block := response.read(128 * 1024):
                received += len(block)
                if received > limit:
                    raise ValueError('更新文件超过大小限制')
                if output:
                    output.write(block)
                else:
                    chunks.append(block)
                if progress:
                    progress(received, size)
        finally:
            if output:
                output.close()
    return b''.join(chunks)


def validate_archive(archive: Path, manifest: dict, platform_key: str) -> None:
    if platform_key not in ('windows-x64', 'macos-arm64'):
        raise ValueError('不支持的更新平台')
    prefix = 'CodexTokenDesktop/' if platform_key == 'windows-x64' else 'CodexTokenDesktop.app/'
    records = manifest.get('files')
    if not isinstance(records, list) or len(records) > 20000:
        raise ValueError('无效的更新文件清单')
    if any(not isinstance(r, dict) or not isinstance(r.get('path'), str) for r in records):
        raise ValueError('无效的更新文件清单')
    declared = {r['path']: r for r in records}
    if len(declared) != len(records):
        raise ValueError('更新文件清单包含重复路径')
    names = set()
    normalized = set()
    node_types = {}
    links = {}
    with zipfile.ZipFile(archive) as package:
        if len(package.infolist()) > 40000 or sum(r.file_size for r in package.infolist()) > MAX_EXPANDED:
            raise ValueError('更新包解压体积超过限制')
        for item in package.infolist():
            name = item.filename
            path = PurePosixPath(name)
            canonical = name[:-1] if item.is_dir() else name
            if canonical != path.as_posix() or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or not (name.startswith(prefix) or (platform_key == 'macos-arm64' and name.startswith('__MACOSX/' + prefix))):
                raise ValueError('更新包包含不安全的路径')
            if platform_key == 'windows-x64' and any(p.endswith((' ', '.')) or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.IGNORECASE) for p in path.parts):
                raise ValueError('更新包包含不安全的 Windows 文件名')
            key = canonical.casefold() if platform_key == 'windows-x64' else canonical
            if key in normalized:
                raise ValueError('更新包包含重复路径')
            normalized.add(key)
            is_link = stat.S_ISLNK(item.external_attr >> 16)
            node_types[key] = 'directory' if item.is_dir() else ('link' if is_link else 'file')
            if item.is_dir() or name.startswith('__MACOSX/'):
                if is_link:
                    raise ValueError('更新包包含无效的目录链接')
                continue
            relative = name[len(prefix):]
            content = package.read(item)
            if relative == 'build-manifest.json' and platform_key == 'windows-x64':
                if json.loads(content) != manifest:
                    raise ValueError('更新包内部版本信息不一致')
                continue
            record = declared.get(relative)
            if record is None:
                raise ValueError('更新包包含未声明的文件')
            names.add(relative)
            if record.get('type') == 'symlink':
                if platform_key == 'windows-x64' or not is_link:
                    raise ValueError('更新包符号链接类型错误')
                link = content.decode('utf-8')
                if link != record.get('target') or not link or link.startswith('/') or '\\' in link or '\x00' in link:
                    raise ValueError('更新包符号链接越界')
                links[canonical] = link
            elif is_link or hashlib.sha256(content).hexdigest() != record.get('sha256'):
                raise ValueError('更新文件校验失败')
        if names != set(declared):
            raise ValueError('更新包缺少必要文件')
        for name in node_types:
            for parent in PurePosixPath(name).parents:
                kind = node_types.get(parent.as_posix())
                if kind in ('file', 'link'):
                    raise ValueError('更新包文件和目录路径冲突')
        # Resolve each link component before processing subsequent '..'. A
        # lexical normpath alone can miss an escape through another link.
        root = prefix.rstrip('/')
        for name, link in links.items():
            components = name.split('/')[:-1] + link.split('/')
            resolved = []
            expansions = 0
            while components:
                part = components.pop(0)
                if part in ('', '.'):
                    continue
                if part == '..':
                    if len(resolved) <= 1:
                        raise ValueError('更新包符号链接越界')
                    resolved.pop()
                    continue
                resolved.append(part)
                if resolved[0] != root:
                    raise ValueError('更新包符号链接越界')
                nested = links.get('/'.join(resolved))
                if nested is not None:
                    expansions += 1
                    if expansions > 40:
                        raise ValueError('更新包符号链接循环')
                    resolved.pop()
                    components = nested.split('/') + components


def validate_installation(target: Path) -> None:
    if not target.is_absolute() or target.parent == target or 'AppTranslocation' in target.parts:
        raise ValueError('请先把应用移到可写的固定目录，再进行更新')
    if sys.platform == 'win32':
        if str(target).startswith('\\\\'):
            raise ValueError('不支持网络安装目录')
        for path in (target, *target.parents):
            if path.exists() and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('安装路径不能经过链接')
        system = Path(os.environ['WINDIR']).resolve()
        if target == system or system in target.parents:
            raise ValueError('不能更新系统目录中的目标')
    else:
        if target.parent == Path('/') or target.parts[1] in ('System', 'usr', 'bin', 'sbin', 'dev', 'private'):
            raise ValueError('请把应用移到可写的应用目录，再进行更新')
        if target.resolve() != target:
            raise ValueError('安装路径不能经过链接')


def process_alive(pid: int) -> bool:
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def validate_job(path: Path, target: Path | None = None) -> dict:
    job = read_json(path)
    identifier = job.get('id', '')
    if not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ValueError('无效的安装任务')
    actual_target = Path(job['target'])
    if target and actual_target != target:
        raise ValueError('安装目标与当前程序不一致')
    stage = actual_target.parent / ('.codex-token-update-' + identifier)
    expected_candidate = stage / 'candidate' / ('CodexTokenDesktop.app' if job['platform'] == 'darwin' else 'CodexTokenDesktop')
    if path != stage / 'job.json' or Path(job['candidate']) != expected_candidate or Path(job['backup']) != actual_target.with_name(actual_target.name + '.rollback-' + identifier):
        raise ValueError('安装任务路径不一致')
    if not actual_target.is_absolute() or actual_target == actual_target.parent or stage.is_symlink():
        raise ValueError('无效的安装目录')
    return job


def confirm_startup(job_path: Path) -> None:
    job_path = job_path.absolute()
    job = validate_job(job_path, installation_dir())
    write_json(job_path.parent / 'ack.json', {'id': job['id'], 'pid': os.getpid()})


class Updater:
    def __init__(self, data_dir: Path, close_window) -> None:
        self._data_dir = data_dir
        self._close_window = close_window
        self._lock = threading.RLock()
        self._selected = None
        self._job_path = None
        self._lock_file = None
        self._info = {'version': (resource_dir() / 'VERSION').read_text(encoding='utf-8').strip(), 'build_number': 0, 'source_commit': None}
        try:
            self._info.update(read_json(resource_dir() / 'build-info.json'))
        except (OSError, ValueError):
            pass
        self._key = 'windows-x64' if sys.platform == 'win32' else 'macos-arm64'
        supported = getattr(sys, 'frozen', False) and ((sys.platform == 'win32' and platform.machine().lower() in ('amd64', 'x86_64')) or (sys.platform == 'darwin' and platform.machine().lower() == 'arm64' and installation_dir().suffix == '.app'))
        self._status = {'state': 'idle' if supported else 'unsupported', 'current_version': self._info['version'], 'latest_version': '', 'progress': 0, 'message': '可检查软件更新' if supported else '源码运行仅支持检查代码；请使用桌面安装包进行一键更新'}
        try:
            pending = read_json(data_dir / 'updater-pending.json')
            result = read_json(Path(pending['job']).parent / 'result.json')
            if result.get('state') == 'error':
                self._status.update(state='error', last_error=result.get('message', '上次更新失败'), message='上次更新失败，原程序已保留。' + result.get('message', ''))
        except (OSError, ValueError, KeyError):
            pass

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, **values) -> None:
        with self._lock:
            self._status.update(values)

    def check(self) -> dict:
        with self._lock:
            if self._status['state'] in ('checking', 'downloading', 'ready', 'installing', 'unsupported'):
                return self.status()
            self._selected = None
            self._status.update(state='checking', message='正在检查 GitHub 更新…', progress=0, last_error='')
            threading.Thread(target=self._check, daemon=True, name='desktop-update-check').start()
            return self.status()

    def _check(self) -> None:
        try:
            release = json.loads(fetch_bytes(API, 2 * 1024 * 1024))
            if not isinstance(release, dict) or not isinstance(release.get('assets'), list) or any(not isinstance(item, dict) for item in release['assets']):
                raise ValueError('无效的 GitHub 发布信息')
            assets = {item['name']: item for item in release['assets']}
            update = json.loads(fetch_bytes(assets['update.json']['browser_download_url'], 2 * 1024 * 1024))
            if not isinstance(update, dict) or update.get('schema') != 1 or not re.fullmatch('[0-9a-f]{40}', update['source_commit']) or not re.fullmatch('[0-9a-f]{64}', update['source_digest']) or not isinstance(update.get('packages'), dict):
                raise ValueError('更新发布信息不完整')
            latest = version_tuple(update['version'])
            current = version_tuple(self._info['version'])
            number = int(update['build_number'])
            if latest < current or update['source_commit'] == self._info.get('source_commit') or (latest == current and number <= int(self._info.get('build_number', 0))):
                self._set(state='current', latest_version=update['version'], message='当前已是最新版本')
                return
            selected = update['packages'][self._key]
            if not isinstance(selected, dict):
                raise ValueError('无效的更新平台信息')
            for field in ('archive', 'manifest'):
                selected[field + '_url'] = assets[selected[field]]['browser_download_url']
                if not safe_asset_url(selected[field + '_url']):
                    raise ValueError('更新下载地址不正确')
            if not re.fullmatch('[0-9a-f]{64}', selected['sha256']) or not re.fullmatch('[0-9a-f]{64}', selected['manifest_sha256']) or not 0 < selected['size'] <= MAX_ARCHIVE:
                raise ValueError('更新文件校验信息不完整')
            with self._lock:
                self._selected = (update, selected)
            label = update['version'] if latest != current else f"{update['version']} · 构建 {number}"
            self._set(state='available', latest_version=label, message='发现新版本，更新后会自动重启；统计数据和设置会保留')
        except HTTPError as error:
            if error.code == 404:
                self._set(state='current', message='仓库暂未发布更新版本')
            else:
                self._set(state='error', message='GitHub 暂时无法访问，请稍后重试')
        except (OSError, URLError, ValueError, KeyError, TypeError) as error:
            self._set(state='error', message='检查更新失败，请稍后重试：' + str(error)[:160])

    def start(self) -> dict:
        with self._lock:
            if self._status['state'] != 'available' or self._selected is None:
                return self.status()
            self._status.update(state='downloading', message='正在下载更新…', progress=0)
            threading.Thread(target=self._download, daemon=True, name='desktop-update-download').start()
            return self.status()

    def _download(self) -> None:
        try:
            update, selected = self._selected
            target = installation_dir()
            validate_installation(target)
            lock_file = target.with_name('.' + target.name + '.update.lock')
            if lock_file.exists():
                lock = read_json(lock_file)
                if process_alive(int(lock['pid'])):
                    raise ValueError('另一个窗口正在更新，请稍后再试')
                lock_file.unlink()
            descriptor = os.open(lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            self._lock_file = lock_file
            write_json(lock_file, {'pid': os.getpid()})
            identifier = uuid.uuid4().hex
            stage = target.parent / ('.codex-token-update-' + identifier)
            stage.mkdir(mode=0o700)
            archive = stage / 'update.zip'
            fetch_bytes(selected['archive_url'], MAX_ARCHIVE, target=archive, progress=lambda n, total: self._set(progress=min(85, int(n * 85 / selected['size']))))
            if archive.stat().st_size != selected['size'] or hashlib.sha256(archive.read_bytes()).hexdigest() != selected['sha256']:
                raise ValueError('下载不完整或校验失败，请重新下载')
            self._set(message='正在校验更新包…', progress=90)
            manifest_bytes = fetch_bytes(selected['manifest_url'], 2 * 1024 * 1024)
            if hashlib.sha256(manifest_bytes).hexdigest() != selected['manifest_sha256']:
                raise ValueError('版本清单校验失败')
            manifest = json.loads(manifest_bytes)
            if manifest['version'] != update['version'] or manifest['source_commit'] != update['source_commit'] or manifest['source_digest'] != update['source_digest'] or manifest['architecture'] != ('x64' if self._key == 'windows-x64' else 'arm64') or manifest['platform'] != sys.platform:
                raise ValueError('更新包与发布版本不一致')
            validate_archive(archive, manifest, self._key)
            candidate_root = stage / 'candidate'
            candidate_root.mkdir()
            if sys.platform == 'darwin':
                subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(candidate_root)], check=True, capture_output=True)
                candidate = candidate_root / 'CodexTokenDesktop.app'
                subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(candidate)], check=True, capture_output=True)
            else:
                with zipfile.ZipFile(archive) as package:
                    package.extractall(candidate_root)
                candidate = candidate_root / 'CodexTokenDesktop'
                portable_config = target / 'providers.json'
                if portable_config.is_file():
                    shutil.copy2(portable_config, candidate / 'providers.json')
            info_path = candidate / ('Contents/Resources/build-info.json' if sys.platform == 'darwin' else '_internal/build-info.json')
            info = read_json(info_path)
            if info['source_commit'] != update['source_commit'] or info['version'] != update['version']:
                raise ValueError('候选程序的版本信息不一致')
            job = {'id': identifier, 'target': str(target), 'candidate': str(candidate), 'backup': str(target.with_name(target.name + '.rollback-' + identifier)), 'parent_pid': os.getpid(), 'platform': sys.platform}
            job_path = stage / 'job.json'
            write_json(job_path, job)
            validate_job(job_path, target)
            script = stage / ('install.ps1' if sys.platform == 'win32' else 'install.sh')
            script.write_text(WINDOWS_HELPER if sys.platform == 'win32' else MACOS_HELPER, encoding='utf-8-sig' if sys.platform == 'win32' else 'utf-8')
            self._data_dir.mkdir(parents=True, exist_ok=True)
            write_json(self._data_dir / 'updater-pending.json', {'job': str(job_path)})
            self._job_path = job_path
            self._set(state='installing', message='更新包已校验，正在关闭并重启软件…', progress=100)
            self._close_window()
        except Exception as error:
            self._job_path = None
            if self._lock_file:
                self._lock_file.unlink(missing_ok=True)
                self._lock_file = None
            self._set(state='error', message='更新未完成，当前程序保持不变：' + str(error)[:180])

    def launch_installer(self) -> None:
        if self._job_path is None:
            return
        job_path = self._job_path
        validate_job(job_path, installation_dir())
        try:
            if sys.platform == 'win32':
                command = [str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(job_path.parent / 'install.ps1'), '-JobPath', str(job_path)]
                helper = subprocess.Popen(command, cwd=job_path.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
            else:
                helper = subprocess.Popen(['/bin/sh', str(job_path.parent / 'install.sh'), str(job_path)], cwd=job_path.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            write_json(job_path.parent / 'result.json', {'state': 'error', 'message': '无法启动安装器：' + str(error)[:160]})
            if self._lock_file:
                self._lock_file.unlink(missing_ok=True)
            command = [str(installation_dir() / ('CodexTokenDesktop.exe' if sys.platform == 'win32' else 'Contents/MacOS/CodexTokenDesktop'))]
            options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if sys.platform == 'win32' else {'start_new_session': True}
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            return
        if self._lock_file:
            write_json(self._lock_file, {'pid': helper.pid, 'job': str(job_path)})
