"""Independent global skills libraries. Skill content is never executed."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from uuid import uuid4


AGENTS = ('codex', 'hermes')
EXTERNAL = 'external:'
MAX_DOC = 2 * 1024 * 1024
_DEVICE = re.compile(r'^(?:con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[0-9\u00b9\u00b2\u00b3])$', re.I)


class SkillError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise SkillError(code, message)


def component(value, *, hidden=False):
    require(isinstance(value, str) and value not in ('', '.', '..')
            and (hidden or not value.startswith('.'))
            and not re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            and value == value.rstrip(' .') and not _DEVICE.fullmatch(value.split('.')[0].rstrip()),
            'unsafe_path', f'Unsafe path component: {value!r}')
    return value


def absolute(value):
    require(isinstance(value, (str, os.PathLike)) and bool(os.fspath(value)),
            'invalid_input', 'A nonempty local path is required')
    path = Path(value).expanduser()
    require('..' not in path.parts and (not path.anchor or path.is_absolute()),
            'unsafe_path', f'Unsafe path: {path}')
    if os.name == 'nt':
        require(not path.drive.startswith('\\\\'), 'unsafe_path', 'Network and device paths are unsupported')
    path = path.absolute()
    for part in path.parts[1:]:
        component(part, hidden=True)
    return path


def info(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def linked(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def plain(path, *, file=False, missing=False):
    for part in (*reversed(path.parents), path):
        st = info(part)
        if st is None and missing:
            return None
        kind = stat.S_ISREG if file and part == path else stat.S_ISDIR
        require(st is not None and not linked(st) and kind(st.st_mode), 'unsafe_path',
                f'Expected a plain filesystem entry: {part}')
    return st


def key(path):
    return tuple(part.casefold() for part in path.parts)


def inside(path, parent):
    return key(path)[:len(parent.parts)] == key(parent)


def overlap(a, b):
    return inside(a, b) or inside(b, a)


def identity(st):
    return [st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode)] if st else None


def entries(path):
    device = plain(path).st_dev

    def walk(parent):
        seen = set()
        for entry in sorted(parent.iterdir(), key=lambda p: p.name):
            name = component(entry.name, hidden=True).casefold()
            require(name not in seen, 'unsafe_path', f'Case-insensitive name collision: {entry}')
            seen.add(name)
            st = entry.lstat()
            is_dir = stat.S_ISDIR(st.st_mode)
            require(not linked(st) and st.st_dev == device
                    and (stat.S_ISREG(st.st_mode) or (is_dir and not entry.is_mount())),
                    'unsafe_path', f'Links, mounts and special files are unsupported: {entry}')
            yield entry, st, is_dir
            if is_dir:
                yield from walk(entry)

    yield from walk(path)


def digest(path):
    h = hashlib.sha256()
    for entry, st, is_dir in entries(path):
        mode = st.st_mode & 0o111 if os.name != 'nt' and not is_dir else 0
        content = None
        if not is_dir:
            with entry.open('rb') as stream:
                content = hashlib.file_digest(stream, 'sha256').hexdigest()
        h.update(json.dumps([entry.relative_to(path).as_posix(), mode, content]).encode() + b'\n')
    return h.hexdigest()


def copy_tree(source, target):
    target.mkdir()
    for entry, st, is_dir in entries(source):
        dest = target / entry.relative_to(source)
        if is_dir:
            dest.mkdir()
        else:
            shutil.copyfile(entry, dest, follow_symlinks=False)
            plain(dest, file=True)
            if os.name != 'nt':
                dest.chmod(0o644 | (st.st_mode & 0o111))


def doc(path, name, *, strict=False):
    st = info(path / name)
    if st is None or linked(st) or not stat.S_ISREG(st.st_mode):
        require(not strict, 'invalid_skill', f'A plain {name} is required: {path}')
        return None
    with (path / name).open('rb') as stream:
        data = stream.read(MAX_DOC + 1)
    require(not strict or len(data) <= MAX_DOC, 'invalid_skill', f'{name} is too large')
    try:
        return data[:MAX_DOC].decode('utf-8-sig', errors='strict' if strict else 'replace')
    except UnicodeError as exc:
        raise SkillError('invalid_skill', f'{name} must be UTF-8') from exc


def metadata(default_name, text):
    fields = {}
    lines = (text or '').splitlines()
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in ('---', '...')), 0)
        for i, line in enumerate(lines[1:end], 1):
            match = re.match(r'^(name|description):\s*(.*)$', line)
            if not match:
                continue
            field, value = match.groups()
            if not value or re.fullmatch(r'[>|][-+]?\d?', value):
                block = []
                for continuation in lines[i + 1:end]:
                    if continuation and not continuation[0].isspace():
                        break
                    block.append(continuation.strip())
                value = ('\n' if value.startswith('|') else ' ').join(block).strip()
            elif value.startswith('"'):
                try:
                    value, _ = json.JSONDecoder().raw_decode(value)
                except ValueError:
                    pass
            elif value.startswith("'") and (quoted := re.match(r"^'((?:[^']|'')*)'", value)):
                value = quoted[1].replace("''", "'")
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
            fields[field] = value
    return {'name': fields.get('name') or default_name, 'description': fields.get('description', '')}


def rename_new(source, target):
    """Avoid POSIX rename replacing an unowned empty destination directory."""
    plain(source.parent)
    plain(target.parent)
    if os.name == 'nt':
        os.rename(source, target)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(source), os.fsencode(target)
    if sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        result = libc.renameat2(-100, old, -100, new, 1)
    elif sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        result = libc.renamex_np(old, new, 0x4)
    else:
        raise SkillError('unsupported_platform', 'Exclusive filesystem rename is unavailable')
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def link_directory(source, target):
    if os.name == 'nt':
        # A junction supports local directory targets without symlink privilege.
        import _winapi

        _winapi.CreateJunction(str(source), str(target))
    else:
        target.symlink_to(source, target_is_directory=True)


class FileTransaction:
    """Stage on the same volume; reverse completed moves before releasing the lock."""

    def __init__(self, marker):
        self.marker = marker
        self.started = False
        self.areas, self.mkdirs, self.moves, self.trash = [], [], [], []
        self.warning = None

    def directory(self, path):
        if plain(path, missing=True) is not None:
            return
        self.directory(path.parent)
        path.mkdir()
        self.mkdirs.append(path)

    def stage(self, parent):
        self.directory(parent)
        area = Path(tempfile.mkdtemp(prefix='.skill-library-', dir=parent))
        self.areas.append((area, identity(area.lstat())))
        return area / 'item'

    def move(self, source, target):
        stamp = identity(source.lstat())
        with self.marker.open('a' if self.started else 'x', encoding='utf-8') as journal:
            self.started = True
            journal.write(json.dumps([str(source), str(target), stamp]) + '\n')
            journal.flush()
            os.fsync(journal.fileno())
        rename_new(source, target)
        self.moves.append((source, target, stamp))

    def swap(self, target, incoming=None, verify=None):
        if verify:
            old = self.stage(target.parent)
            verify(target)
            self.move(target, old)
            verify(old)
            self.trash.append((old, verify))
        if incoming is not None:
            self.move(incoming, target)

    def finish(self, committed):
        try:
            if committed:
                for target, verify in self.trash:
                    verify(target)
            else:
                for source, target, stamp in reversed(self.moves):
                    require(identity(info(target)) == stamp, 'recovery_required',
                            f'Transaction entry changed: {target}')
                    rename_new(target, source)
            for area, stamp in reversed(self.areas):
                require(identity(plain(area)) == stamp, 'recovery_required', f'Staging changed: {area}')
                shutil.rmtree(area)
            if not committed:
                for path in reversed(self.mkdirs):
                    try:
                        path.rmdir()
                    except OSError:
                        pass
            if self.started:
                self.marker.unlink()
        except (OSError, SkillError) as exc:
            message = f'Inspect transaction staging at {self.marker}: {exc}'
            if committed:
                self.warning = message
            else:
                raise SkillError('recovery_required', message) from exc


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def strings(value, field):
    require(isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value),
            'invalid_input', f'{field} must be a list of nonempty strings')
    return list(dict.fromkeys(value))


def title(value):
    require(isinstance(value, str) and value.strip(), 'invalid_input', 'A preset name is required')
    return value.strip()


def serial_source(value):
    require(value is None or isinstance(value, (str, dict)), 'invalid_input', 'Invalid source metadata')
    try:
        return json.loads(encode(value))
    except (TypeError, ValueError) as exc:
        raise SkillError('invalid_input', 'Source must be JSON serializable') from exc


class SkillLibrary:
    """A single agent within a shared data directory. Public methods must not be reentered.

    state keys: skills (list), presets (list of {name, ids}), root (str).
    Read actions return {ok, skill/history/root}; mutations return {ok, state}.
    Failed actions return {ok: False, code, error}. Direct import/update methods
    return a skill record or raise. The agent selection never comes from payloads.
    """

    def __init__(self, data_dir, agent):
        require(agent in AGENTS, 'invalid_agent', 'Agent must be codex or hermes')
        self.data_dir = absolute(data_dir)
        self.agent = agent
        self._base = self.data_dir / 'skills-library'
        self._private = self._base / agent
        self._files = self._private / 'files'
        self._versions = self._private / 'versions'
        self._marker = self._private / 'pending.jsonl'
        self._db = self._base / 'library.sqlite3'

    @staticmethod
    def _default(agent):
        return absolute(os.environ.get(f'{agent.upper()}_HOME') or Path.home() / f'.{agent}') / 'skills'

    @contextmanager
    def _locked(self, write=False):
        require(not any(overlap(self._base, self._default(agent)) for agent in AGENTS), 'unsafe_path',
                'Private library storage overlaps an agent installation root')
        plain(self._private, missing=True)
        self._private.mkdir(parents=True, exist_ok=True)
        plain(self._private)
        for suffix in ('', '-journal', '-wal', '-shm'):
            path = self._db.with_name(self._db.name + suffix)
            st = plain(path, file=True, missing=True)
            require(st is None or st.st_nlink == 1, 'unsafe_path', f'Hard-linked database entry: {path}')
        conn = sqlite3.connect(self._db, isolation_level=None, timeout=10)
        txn = FileTransaction(self._marker)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE IF NOT EXISTS agent_state (agent TEXT PRIMARY KEY, data TEXT NOT NULL)')
            row = conn.execute('SELECT data FROM agent_state WHERE agent=?', (self.agent,)).fetchone()
            original = row[0] if row else None
            data = json.loads(original) if row else None
            if data is None:
                data = {'library_id': str(uuid4()), 'root': None, 'skills': {}, 'presets': {}}
            other = next(agent for agent in AGENTS if agent != self.agent)
            peer = conn.execute('SELECT data FROM agent_state WHERE agent=?', (other,)).fetchone()
            peer_data = json.loads(peer[0]) if peer else None
            peer_root = None if not peer_data else peer_data['root'] or next((
                rec['deploy']['root'] for rec in peer_data['skills'].values() if rec['deploy']), None)
            if write:
                require(info(self._marker) is None, 'recovery_required',
                        f'An interrupted file transaction needs inspection: {self._marker}')
            try:
                yield data, peer_root, txn
                updated = encode(data)
                if updated != original:
                    conn.execute('INSERT OR REPLACE INTO agent_state VALUES (?, ?)', (self.agent, updated))
                conn.commit()
            except BaseException:
                txn.finish(False)
                conn.rollback()
                raise
            txn.finish(True)
        finally:
            conn.close()

    @property
    def library_id(self):
        with self._locked() as (data, _, _txn):
            return data['library_id']

    def _root(self, data, peer_root):
        deployed = next((rec['deploy']['root'] for rec in data['skills'].values() if rec['deploy']), None)
        return self._checked_root(data['root'] or deployed or self._default(self.agent), peer_root)

    def _checked_root(self, value, peer_root):
        root = absolute(value)
        home = absolute(Path.home())
        other = next(agent for agent in AGENTS if agent != self.agent)
        forbidden = [self._base, self._default(other), home / f'.{other}' / 'skills']
        if peer_root:
            forbidden.append(absolute(peer_root))
        require(root != Path(root.anchor) and not inside(home, root)
                and not any(overlap(root, path) for path in forbidden)
                and not any(inside(self._default(agent).parent, root) for agent in AGENTS),
                'unsafe_root', f'Root overlaps a home, another library, or another agent: {root}')
        for var in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)') if os.name == 'nt' else ():
            if os.environ.get(var):
                require(not overlap(root, absolute(os.environ[var])), 'unsafe_root', f'Protected root: {root}')
        if os.name != 'nt':
            require(not any(inside(root, Path(system)) for system in (
                '/etc', '/usr', '/proc', '/dev', '/sys', '/bin', '/sbin', '/lib', '/boot', '/System')),
                'unsafe_root', f'Protected root: {root}')
        plain(root, missing=True)
        return root

    def _input(self, value, peer_root):
        path = absolute(value)
        other = next(agent for agent in AGENTS if agent != self.agent)
        blocked = [self._base, self._default(other), absolute(Path.home()) / f'.{other}' / 'skills']
        if peer_root:
            blocked.append(absolute(peer_root))
        require(not any(overlap(path, root) for root in blocked), 'unsafe_path',
                f'Input overlaps a private library or another agent: {path}')
        plain(path)
        doc(path, 'SKILL.md', strict=True)
        return path

    def _managed(self, data, skill_id, *, deleted=False):
        require(isinstance(skill_id, str), 'invalid_input', 'A string skill id is required')
        require(not skill_id.startswith(EXTERNAL), 'external_skill', 'External installations are read-only')
        component(skill_id)
        rec = data['skills'].get(skill_id)
        require(rec is not None and (deleted or not rec['deleted']), 'not_found', f'Unknown skill: {skill_id}')
        return rec

    @staticmethod
    def _verify_tree(path, sha, stamp=None):
        actual = plain(path, missing=True)
        require(actual is not None and (stamp is None or identity(actual) == stamp) and digest(path) == sha,
                'modified', f'Files differ from the recorded version; preserve your edits before retrying: {path}')

    def _verify_deploy(self, path, deployment):
        plain(path.parent)
        st = info(path)
        require(st is not None and identity(st) == deployment['identity'], 'modified',
                f'Deployment was replaced outside the library: {path}')
        if deployment['mode'] == 'link':
            require(linked(st) and os.readlink(path) == deployment['target'], 'modified',
                    f'Deployment link was changed: {path}')
        else:
            self._verify_tree(path, deployment['digest'], deployment['identity'])

    @staticmethod
    def _target(root, skill_id):
        return root / component(skill_id)

    @staticmethod
    def _vacant(target):
        require(info(target) is None and not any(p.name.casefold() == target.name.casefold()
                for p in target.parent.iterdir()), 'target_exists', f'Existing target is not owned: {target}')

    def _install(self, root, rec, txn, mode, *, change=False, source=None):
        skill_id = rec['id']
        source = self._files / skill_id if source is None else source
        target = self._target(root, skill_id)
        old = rec['deploy']
        verify = None
        if old:
            require(key(root) == key(Path(old['root'])), 'unsafe_root', 'Deployment belongs to a different root')
            if info(target) is not None:
                self._verify_deploy(target, old)
                if old['mode'] == mode and not change:
                    return
                verify = lambda p: self._verify_deploy(p, old)
        if verify is None:
            external, conflicts = self._external_installation(root, rec)
            require(not conflicts, 'install_conflict', f"Existing Codex skill: {', '.join(conflicts)}")
            if external:
                return
            txn.directory(root)
            self._vacant(target)
        staged = txn.stage(root)
        new = {'root': str(root), 'mode': mode}
        if mode == 'copy':
            copy_tree(source, staged)
            new['digest'] = digest(staged)
            require(new['digest'] == rec['digest'], 'modified', 'Library content changed while copying')
        else:
            try:
                link_directory(self._files / skill_id, staged)
            except OSError as exc:
                raise SkillError('link_unavailable', f'Could not create a directory link; choose copy mode: {exc}') from exc
            new['target'] = os.readlink(staged)
        new['identity'] = identity(staged.lstat())
        txn.swap(target, staged, verify)
        rec['deploy'] = new

    def _disable(self, root, rec, txn):
        old = rec['deploy']
        if old is None:
            return
        require(key(root) == key(Path(old['root'])), 'unsafe_root', 'Deployment belongs to a different root')
        target = self._target(root, rec['id'])
        if info(target) is not None:
            txn.swap(target, verify=lambda p: self._verify_deploy(p, old))
        rec['deploy'] = None

    def _deployment_status(self, root, rec):
        deployment = rec['deploy']
        if not deployment or info(root / rec['id']) is None:
            return False, False
        try:
            self._verify_deploy(root / rec['id'], deployment)
            return True, False
        except (OSError, SkillError):
            st = info(root / rec['id'])
            return identity(st) == deployment['identity'], True

    def _external_installation(self, root, rec, externals=None, *, owned=False):
        if self.agent != 'codex':
            return None, []
        target = root / rec['id']
        if externals is None:
            externals = self._externals(root)

        origin = Path(rec['origin']) if rec['origin'] else None
        # The import preview stages local files before import_path. In those
        # records origin is temporary, but source retains the scanned location.
        local = next((item['source'] for item in reversed(rec['history'])
                      if isinstance(item['source'], dict) and item['source'].get('kind') == 'local'), {})
        source_path, subdir = local.get('path'), local.get('subdir')
        source = Path(source_path) if isinstance(source_path, str) and isinstance(subdir, str) else None
        names = {rec['id'].casefold(), rec['name'].casefold()}
        installation, conflicts = None, []
        for row in externals:
            path = Path(row['path'])
            if owned and (path == target or target in path.parents):
                continue
            from_source = source is not None and (path == source / subdir or
                                                   (not subdir and path / 'SKILL.md' == source))
            if not owned and path == origin:
                if installation is not None:
                    conflicts.append(installation)
                installation = row['path']
            elif not owned and from_source and installation is None:
                installation = row['path']
            elif names.intersection((path.name.casefold(), row['name'].casefold())):
                conflicts.append(row['path'])
        return installation, conflicts

    def _record(self, root, rec, *, deployment_status=None, externals=None):
        installed, modified = (self._deployment_status(root, rec) if deployment_status is None
                               else deployment_status)
        external, conflicts = self._external_installation(root, rec, externals, owned=installed)
        return {field: rec[field] for field in ('id', 'name', 'description', 'tags', 'source', 'version', 'digest')} | {
            'managed': True, 'external': False, 'installed': installed or external is not None, 'modified': modified,
            'mode': rec['deploy']['mode'] if rec['deploy'] else None,
            'path': str(self._files / rec['id']), 'installed_path': str(root / rec['id']) if installed else external,
            'installed_externally': external is not None, 'install_conflicts': conflicts,
        }

    def _externals(self, root, owned=()):
        if info(root) is None:
            return
        device = root.lstat().st_dev

        def walk(parent):
            for path in sorted(parent.iterdir(), key=lambda p: p.name.casefold()):
                if path in owned or (path.name.startswith('.') and path.name != '.system'):
                    continue
                st = info(path)
                if st is None or st.st_dev != device:
                    continue
                is_link = linked(st)
                if is_link:
                    # Do not follow third-party links to read files or enumerate descendants.
                    if not path.is_dir():
                        continue
                    text = None
                elif stat.S_ISDIR(st.st_mode) and not path.is_mount():
                    text = doc(path, 'SKILL.md')
                    if text is None:
                        yield from walk(path)
                        continue
                else:
                    continue
                relative = path.relative_to(root).as_posix()
                yield {'id': EXTERNAL + relative, **metadata(path.name, text), 'installed': True, 'tags': [],
                       'source': str(path), 'managed': False, 'external': True, 'modified': False,
                       'mode': 'link' if is_link else None, 'path': str(path), 'installed_path': str(path),
                       'relative_path': relative, 'version': None, 'digest': None}

        yield from walk(root)

    @staticmethod
    def _presets(data):
        return [{'name': name, 'ids': data['presets'][name]} for name in sorted(data['presets'], key=str.casefold)]

    def _state(self, data, root):
        active = [rec for rec in data['skills'].values() if not rec['deleted']]
        deployments = {rec['id']: self._deployment_status(root, rec) for rec in active}
        owned = {root / skill_id for skill_id, (installed, _) in deployments.items() if installed}
        externals = list(self._externals(root, owned if self.agent == 'hermes' else ()))
        records = [self._record(root, rec, deployment_status=deployments[rec['id']], externals=externals)
                   for rec in active]
        records.extend(row for row in externals if root / row['relative_path'].partition('/')[0] not in owned)
        records.sort(key=lambda rec: (rec['name'].casefold(), rec['id']))
        return {'skills': records, 'presets': self._presets(data), 'root': str(root)}

    def state(self):
        with self._locked() as (data, peer, txn):
            return self._state(data, self._root(data, peer))

    def _find(self, data, root, skill_id):
        require(isinstance(skill_id, str), 'invalid_input', 'A string skill id is required')
        if skill_id.startswith(EXTERNAL):
            rec = next((rec for rec in self._state(data, root)['skills'] if rec['id'] == skill_id), None)
            require(rec is not None, 'not_found', f'Unknown skill: {skill_id}')
            return rec
        return self._record(root, self._managed(data, skill_id))

    def skill_path(self, skill_id):
        with self._locked() as (data, peer, txn):
            return self._record_path(self._find(data, self._root(data, peer), skill_id))

    @staticmethod
    def _record_path(rec):
        path = Path(rec['path'])
        plain(path)
        return path

    def _detail(self, data, root, skill_id):
        rec = self._find(data, root, skill_id)
        path = None if rec['external'] and rec['mode'] == 'link' else self._record_path(rec)
        return rec | {field: doc(path, name) if path else None
                      for field, name in (('skill_md', 'SKILL.md'), ('readme_md', 'README.md'))}

    def _ingest(self, data, root, txn, skill_id, path, source, *, reason, origin=None):
        previous = data['skills'].get(skill_id)
        live = self._files / skill_id
        verify = None
        if previous and not previous['deleted']:
            sha = previous['digest']
            if key(live) == key(path) and reason == 'import':
                sha = digest(live)  # Explicitly record edits made through a managed link.
            verify = lambda p: self._verify_tree(p, sha, previous['identity'])
            verify(live)
        work = txn.stage(self._files)
        copy_tree(path, work)
        attributes = metadata(skill_id, doc(work, 'SKILL.md', strict=True))
        checksum = digest(work)
        require(digest(path) == checksum, 'modified', 'Source changed while it was copied')
        if previous and not previous['deleted'] and previous['digest'] == checksum and previous['source'] == source:
            return self._record(root, previous)
        history = previous['history'][:] if previous else []
        version = history[-1]['version'] + 1 if history else 1
        history.append({'version': version, 'digest': checksum, 'source': source, **attributes, 'reason': reason,
                        'created_at': datetime.now(timezone.utc).isoformat()})
        snapshot = txn.stage(self._versions / skill_id)
        copy_tree(work, snapshot)
        txn.swap(self._versions / skill_id / str(version), snapshot)
        rec = {'id': skill_id, 'version': version, 'digest': checksum, 'source': source, **attributes,
               'origin': origin, 'deleted': False, 'identity': identity(work.lstat()),
               'tags': previous['tags'] if previous else [], 'history': history,
               'deploy': previous['deploy'] if previous else None}
        if rec['deploy'] and info(root / skill_id) is not None:
            self._verify_deploy(root / skill_id, rec['deploy'])
            if rec['deploy']['mode'] == 'copy' and checksum != previous['digest']:
                self._install(root, rec, txn, 'copy', change=True, source=work)
        txn.swap(live, work, verify)
        data['skills'][skill_id] = rec
        return self._record(root, rec)

    def import_path(self, path, source=None):
        with self._locked(write=True) as (data, peer, txn):
            root = self._root(data, peer)
            path = absolute(path)
            skill_id = component(path.name)
            rec = data['skills'].get(skill_id)
            require(not any(other.casefold() == skill_id.casefold() and other != skill_id for other in data['skills']),
                    'duplicate_id', f'Case-insensitive skill ID conflict: {skill_id}')
            live = self._files / skill_id
            own = rec and not rec['deleted'] and key(path) == key(live)
            path = live if own else self._input(path, peer)
            if rec and not rec['deleted'] and not own:
                require(rec['origin'] == str(path) or digest(path) == rec['digest'], 'duplicate_id',
                        f'Skill ID already exists: {skill_id}; use an explicit update')
            provenance = rec['source'] if own and source is None else serial_source(
                str(path) if source is None else source)
            return self._ingest(data, root, txn, skill_id, path, provenance, reason='import',
                                origin=rec['origin'] if own else str(path))

    def _replace(self, data, peer, root, txn, skill_id, path, source, validate_current=None):
        rec = self._managed(data, skill_id)
        destination = self._files / skill_id
        if validate_current is not None:
            validate_current(destination, self._record(root, rec))
        path = self._input(path, peer)
        return self._ingest(data, root, txn, skill_id, path,
                            rec['source'] if source is None else serial_source(source),
                            reason='update', origin=rec['origin'])

    def replace_skill(self, skill_id, path, source=None, *, validate_current=None):
        """Replace content for an existing ID; callback runs inside the non-reentrant lock."""
        with self._locked(write=True) as (data, peer, txn):
            return self._replace(data, peer, self._root(data, peer), txn, skill_id, path, source, validate_current)

    @staticmethod
    def _error(exc):
        return {'ok': False, 'code': getattr(exc, 'code', 'target_exists' if isinstance(exc, FileExistsError)
                                             else 'io_error'), 'error': str(exc)}

    @staticmethod
    def _mode(payload):
        mode = payload.get('mode', 'copy')
        if mode == 'symlink':
            mode = 'link'
        require(mode in ('copy', 'link'), 'invalid_input', 'mode must be copy or link')
        return mode

    @staticmethod
    def _version(value):
        if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,17}', value):
            value = int(value)
        require(type(value) is int and value >= 1, 'invalid_input', 'A positive version is required')
        return value

    def _remove(self, data, root, txn, rec):
        self._disable(root, rec, txn)
        skill_id = rec['id']
        txn.swap(self._files / skill_id, verify=lambda path: self._verify_tree(path, rec['digest'], rec['identity']))
        rec['deleted'] = True
        for ids in data['presets'].values():
            if skill_id in ids:
                ids.remove(skill_id)

    def _run(self, data, peer, txn, op, payload):
        if op == 'root' and 'path' in payload:
            selected = payload['path']
            new = self._checked_root(self._default(self.agent) if selected is None else selected, peer)
            require(all(not rec['deploy'] or key(new) == key(Path(rec['deploy']['root']))
                        for rec in data['skills'].values()), 'deployments_active',
                    'Disable managed deployments before changing the root')
            data['root'] = None if selected is None else str(new)
            return {'root': str(new), 'state': self._state(data, new)}
        root = self._root(data, peer)
        skill_id = payload.get('id')
        if op == 'detail':
            return {'skill': self._detail(data, root, skill_id)}
        if op == 'history':
            recs = (self._managed(data, skill_id, deleted=True),) if skill_id is not None else data['skills'].values()
            history = [{'id': rec['id'], **row, 'version_id': row['version'],
                        'current': not rec['deleted'] and rec['version'] == row['version']}
                       for rec in recs for row in rec['history']]
            return {'history': sorted(history, key=lambda row: row['created_at'], reverse=True)}
        if op == 'root':
            return {'root': str(root)}
        enabled = payload.get('enabled', True)
        require(type(enabled) is bool, 'invalid_input', 'enabled must be a boolean')
        extra = {}
        if op == 'rollback':
            rec = self._managed(data, skill_id, deleted=True)
            version = self._version(payload.get('version'))
            item = next((row for row in rec['history'] if row['version'] == version), None)
            require(item is not None, 'not_found', f'Unknown version: {version}')
            path = self._versions / skill_id / str(version)
            self._verify_tree(path, item['digest'])
            extra['skill'] = self._ingest(data, root, txn, skill_id, path, item['source'], reason='rollback',
                                          origin=rec['origin'])
        elif op == 'update':
            rec = self._managed(data, skill_id)
            if 'version' in payload:
                require(rec['version'] == self._version(payload['version']), 'library_changed',
                        'The skill changed after the update was prepared')
            extra['skill'] = self._replace(data, peer, root, txn, skill_id, payload.get('path'), payload.get('source'))
        elif op == 'preset_save':
            name = title(payload.get('name'))
            ids = strings(payload['ids'], 'ids') if 'ids' in payload else [
                rec['id'] for rec in data['skills'].values() if self._deployment_status(root, rec)[0]]
            for member in ids:
                self._managed(data, member)
            data['presets'][name] = ids
        elif op == 'preset_apply':
            name = title(payload.get('name'))
            ids = data['presets'].get(name)
            require(ids is not None, 'not_found', f'Unknown preset: {name}')
            for member in ids:
                rec = self._managed(data, member)
                if enabled:
                    mode = self._mode(payload) if 'mode' in payload or not rec['deploy'] else rec['deploy']['mode']
                    self._verify_tree(self._files / member, rec['digest'], rec['identity'])
                    self._install(root, rec, txn, mode)
                else:
                    self._disable(root, rec, txn)
        elif op == 'preset_delete':
            name = title(payload.get('name'))
            require(name in data['presets'], 'not_found', f'Unknown preset: {name}')
            del data['presets'][name]
        elif op in ('install', 'disable', 'delete', 'tags'):
            ids = strings(payload['ids'], 'ids') if 'ids' in payload else [skill_id]
            require(ids, 'invalid_input', 'At least one skill id is required')
            for rec in [self._managed(data, item) for item in ids]:
                if op == 'tags':
                    rec['tags'] = strings(payload.get('tags'), 'tags')
                elif op == 'delete':
                    self._remove(data, root, txn, rec)
                elif op == 'disable' or not enabled:
                    require(not self._record(root, rec)['installed_externally'], 'external_skill',
                            'External installations are read-only')
                    self._disable(root, rec, txn)
                else:
                    self._verify_tree(self._files / rec['id'], rec['digest'], rec['identity'])
                    mode = self._mode(payload) if 'mode' in payload or not rec['deploy'] else rec['deploy']['mode']
                    self._install(root, rec, txn, mode)
        else:
            raise SkillError('unknown_action', f'Unknown action: {op!r}')
        return extra | {'state': self._state(data, root)}

    def action(self, op, payload=None):
        try:
            if payload is None:
                payload = {}
            require(isinstance(payload, dict), 'invalid_input', 'Payload must be an object')
            write = op not in ('detail', 'history') and (op != 'root' or 'path' in payload)
            with self._locked(write=write) as (data, peer, txn):
                result = self._run(data, peer, txn, op, payload)
            return {'ok': True, **result} | ({'warning': txn.warning} if txn.warning else {})
        except (SkillError, OSError, sqlite3.Error) as exc:
            return self._error(exc)

    def _export(self, data):
        files = {}
        manifest = {'schema': 1, 'skills': [], 'presets': self._presets(data)}
        for skill_id, rec in sorted(data['skills'].items()):
            if rec['deleted']:
                continue
            path = self._files / skill_id
            self._verify_tree(path, rec['digest'], rec['identity'])
            dirs, executable = [], []
            windows_executable = set(rec.get('portable_executable', []))
            for entry, st, is_dir in entries(path):
                relative = entry.relative_to(path).as_posix()
                if is_dir:
                    dirs.append(relative)
                else:
                    files[f'skills/{skill_id}/{relative}'] = entry.read_bytes()
                    is_executable = relative in windows_executable if os.name == 'nt' else bool(st.st_mode & 0o111)
                    if is_executable:
                        executable.append(relative)
            self._verify_tree(path, rec['digest'], rec['identity'])
            manifest['skills'].append({'id': skill_id, 'tags': rec['tags'],
                                       'dirs': sorted(dirs), 'executable': sorted(executable)})
        return {'manifest.json': encode(manifest).encode(), **files}

    def export_portable(self):
        """Export the current managed library only, without local paths or provenance metadata."""
        with self._locked() as (data, _, _txn):
            return self._export(data)

    @staticmethod
    def _relative(name):
        return [component(part, hidden=True) for part in name.split('/')]

    def _portable_input(self, files, txn):
        require(isinstance(files, dict) and all(isinstance(k, str) and isinstance(v, bytes)
                                                for k, v in files.items()),
                'invalid_snapshot', 'Expected a mapping of relative names to bytes')
        try:
            manifest = json.loads(files['manifest.json'].decode('utf-8'))
        except (KeyError, UnicodeError, ValueError) as exc:
            raise SkillError('invalid_snapshot', 'Invalid or missing manifest.json') from exc
        require(isinstance(manifest, dict) and set(manifest) == {'schema', 'skills', 'presets'}
                and type(manifest['schema']) is int and manifest['schema'] == 1
                and isinstance(manifest['skills'], list) and isinstance(manifest['presets'], list),
                'invalid_snapshot', 'Unsupported portable manifest')
        staged, skill_keys, covered = {}, set(), {'manifest.json'}
        grouped = {}
        for name in files.keys() - covered:
            parts = self._relative(name)
            require(len(parts) >= 3 and parts[0] == 'skills', 'invalid_snapshot', f'Unexpected file: {name}')
            grouped.setdefault(parts[1], []).append((name, parts[2:]))
        for item in manifest['skills']:
            require(isinstance(item, dict) and set(item) == {'id', 'tags', 'dirs', 'executable'},
                    'invalid_snapshot', 'Malformed skill entry')
            skill_id = component(item['id'])
            require(skill_id.casefold() not in skill_keys, 'invalid_snapshot', f'Duplicate skill ID: {skill_id}')
            skill_keys.add(skill_id.casefold())
            tags = strings(item['tags'], 'tags')
            dirs = [self._relative(directory) for directory in strings(item['dirs'], 'dirs')]
            executables = set(strings(item['executable'], 'executable'))
            work = txn.stage(self._private)
            work.mkdir()
            paths = set()
            for parts in sorted(dirs, key=lambda parts: (len(parts), parts)):
                relative = tuple(part.casefold() for part in parts)
                target = work.joinpath(*parts)
                require(relative not in paths and target.parent.is_dir(), 'invalid_snapshot', 'Invalid directory listing')
                target.mkdir()
                paths.add(relative)
            written = set()
            for name, parts in sorted(grouped.get(skill_id, [])):
                relative = tuple(part.casefold() for part in parts)
                target = work.joinpath(*parts)
                require(relative not in paths and target.parent.is_dir(), 'invalid_snapshot', 'Invalid file listing')
                with target.open('xb') as stream:
                    stream.write(files[name])
                relative_text = '/'.join(parts)
                if os.name != 'nt' and relative_text in executables:
                    target.chmod(0o755)
                written.add(relative_text)
                paths.add(relative)
                covered.add(name)
            require(executables.issubset(written) and 'SKILL.md' in written, 'invalid_snapshot', 'Invalid skill files')
            doc(work, 'SKILL.md', strict=True)
            staged[skill_id] = (work, tags, sorted(executables))
        require(covered == set(files), 'invalid_snapshot', 'Snapshot contains unlisted files')
        presets = {}
        for item in manifest['presets']:
            require(isinstance(item, dict) and set(item) == {'name', 'ids'}, 'invalid_snapshot', 'Malformed preset')
            name = title(item['name'])
            ids = strings(item['ids'], 'ids')
            require(name not in presets and all(skill_id in staged for skill_id in ids),
                    'invalid_snapshot', 'Duplicate preset name or missing preset skill')
            presets[name] = ids
        return staged, presets

    def import_portable(self, files, *, expected_current=None):
        """Replace active library state under one lock. Never touch agent deployments."""
        try:
            with self._locked(write=True) as (data, peer, txn):
                if expected_current is not None:
                    require(self._export(data) == expected_current, 'library_changed', 'Library changed after export')
                root = self._root(data, peer)
                staged, presets = self._portable_input(files, txn)
                records = data['skills']
                old_ids = {skill_id.casefold(): skill_id for skill_id in records}
                for skill_id in staged:
                    require(old_ids.get(skill_id.casefold(), skill_id) == skill_id, 'duplicate_id',
                            f'Case-insensitive skill ID conflict: {skill_id}')
                hashes = {skill_id: digest(work) for skill_id, (work, _, _) in staged.items()}
                for skill_id, rec in list(records.items()):
                    if rec['deleted']:
                        continue
                    self._verify_tree(self._files / skill_id, rec['digest'], rec['identity'])
                    require(not rec['deploy'] or hashes.get(skill_id) == rec['digest'], 'deployments_active',
                            f'Disable {skill_id} before replacing it from a snapshot')
                    if skill_id not in staged:
                        self._remove(data, root, txn, rec)
                for skill_id, (path, tags, executable) in staged.items():
                    rec = records.get(skill_id)
                    if not rec or rec['deleted'] or rec['digest'] != hashes[skill_id]:
                        self._ingest(data, root, txn, skill_id, path, None, reason='restore')
                    records[skill_id]['tags'] = tags
                    records[skill_id]['portable_executable'] = executable
                data['presets'] = presets
                result = self._state(data, root)
            return {'ok': True, 'state': result} | ({'warning': txn.warning} if txn.warning else {})
        except (SkillError, OSError, sqlite3.Error) as exc:
            return self._error(exc)
