"""Opt-in desktop sync worker. No worker or network activity on import."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Lock, RLock, Thread, current_thread
import time
from uuid import UUID
from weakref import WeakValueDictionary

import skills_backup as backup
from skills_core import SkillLibrary, SkillError, absolute, plain


AGENTS = ('codex', 'hermes')
OPS = frozenset({'autosync_status', 'autosync_set', 'autosync_resume'})
_PAUSE = {'sync_conflict', 'sync_conflict_stale', 'remote_missing', 'remote_changed', 'deployments_active',
          'concurrent_changes'}
_registry = WeakValueDictionary()
_registry_lock = Lock()


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _failure(code, message):
    return {'ok': False, 'code': code, 'error': message}


@dataclass(frozen=True)
class Observation:
    connection: str | None
    digest: str | None
    dirty: bool = False


def _continue(cancelled):
    if cancelled.is_set():
        raise backup._Error('cancelled', 'Automatic sync stopped')


class _CancellableGit(backup._Git):
    def __init__(self, cancelled, *args):
        self._cancelled = cancelled
        _continue(cancelled)
        super().__init__(*args)

    def run(self, *args, **kwargs):
        _continue(self._cancelled)
        return super().run(*args, **kwargs)


class BackupAdapter:
    """Reuse backup's lock, authorization checks, CAS, and conflict handling.

    Read the fingerprint and run a transfer under the *same* lock as manual
    sync, so a remote reconfiguration cannot redirect an authorized transfer.
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir

    @contextmanager
    def _locked(self, agent):
        lib = SkillLibrary(self.data_dir, agent)
        library_id = UUID(lib.library_id).hex
        root = backup._directory(backup._directory(backup._directory(self.data_dir, 'skills-backup'),
                                                   agent), library_id)
        with backup._operation_lock(root):
            yield lib, library_id, root, backup._read_sync(root)

    @staticmethod
    def _connection(library_id, cfg):
        if cfg is None:
            return None
        connection = [library_id, cfg['remote_url'], cfg.get('credential_helper')]
        return hashlib.sha256(json.dumps(connection, sort_keys=True).encode()).hexdigest()

    def observe(self, agent):
        with self._locked(agent) as (lib, library_id, _root, cfg):
            if cfg is None:
                return Observation(None, None)
            files = backup._export(lib)
            digest = backup._digest(files)
            dirty = digest != cfg['digest'] if cfg['digest'] is not None else not backup._is_empty(files)
            return Observation(self._connection(library_id, cfg), digest, dirty)

    def transfer(self, agent, op, observed, cancelled):
        try:
            _continue(cancelled)
            with self._locked(agent) as (lib, library_id, root, cfg):
                _continue(cancelled)
                if self._connection(library_id, cfg) != observed.connection or cfg is None:
                    return _failure('connection_changed', 'Confirm automatic sync for the new connection')
                if op not in ('sync_push', 'sync_pull'):
                    raise backup._Error('invalid_operation', 'Automatic sync can only push or pull')
                files = backup._export(lib)
                if backup._digest(files) != observed.digest:
                    return _failure('library_changed', 'The library changed after inspection')
                return self._transfer(lib, op, root, agent, library_id, cfg, files, observed, cancelled)
        except backup._Error as exc:
            return {'ok': False, 'code': exc.code, **exc.details}

    @staticmethod
    def _transfer(lib, op, root, agent, library_id, cfg, files, observed, cancelled):
        # Background tasks do not merge divergent snapshots; the interactive
        # sync workflow owns all multi-sided changes and conflict resolution.
        exe = backup.shutil.which('git')
        if exe is None:
            raise backup._Error('git_unavailable', 'Git is not available')
        remote_url, protocol = backup._remote(cfg['remote_url'])
        local_digest, base = observed.digest, cfg['digest']
        with tempfile.TemporaryDirectory(prefix='auto-git-', dir=root) as temp:
            git = _CancellableGit(cancelled, exe, Path(temp), remote_url, protocol, agent,
                                  cfg.get('credential_helper'))
            head, remote_digest, remote_files = git.fetch(agent)
            _continue(cancelled)
            local_changed = local_digest != base if base is not None else not backup._is_empty(files)
            if head is None and base is not None:
                raise backup._Error('remote_missing', 'The previously synced branch no longer exists')
            if head is not None and remote_digest != local_digest and local_changed and remote_digest != base:
                common = git.ancestor(head, cfg, agent)
                _continue(cancelled)
                merged, details, _ = backup._merge_portable(common, files, remote_files)
                if merged is not None:
                    raise backup._Error('concurrent_changes', 'Review the two changes using manual sync')
                raise backup._Error('sync_conflict', 'Both snapshots were preserved',
                                    conflict=backup._conflict(head, local_digest, details))
            if op == 'sync_push' and head is not None and remote_digest not in (local_digest, base):
                raise backup._Error('remote_ahead', 'Pull remote changes before pushing')
            if op == 'sync_pull' and (head is None or (local_changed and local_digest != remote_digest)):
                return {'ok': True, 'changed': False}
            changed = local_digest != remote_digest
            if changed and op == 'sync_push':
                if backup._export(lib) != files:
                    raise backup._Error('library_changed', 'The library changed after inspection')
                package = backup._pack(files, backup._sync_manifest(agent, local_digest))
                _continue(cancelled)
                head = git.push(package, head)
            elif changed:
                safety = backup._snapshot(root, agent, library_id, files, 'before_pull')
                _continue(cancelled)
                backup._replace(lib, remote_files, files, safety)
                local_digest = remote_digest
        # Persist the baseline even if stop arrived during a completed transfer.
        baseline = {**cfg, 'revision': head, 'digest': local_digest, 'last_synced_at': backup._now()}
        try:
            backup._atomic(root / 'sync.json', backup._json(baseline))
        except OSError as exc:
            raise backup._Error('sync_state_not_saved', 'Could not record the completed sync') from exc
        return {'ok': True, 'changed': changed}


@dataclass
class _State:
    connection: str | None = None
    pause: str | None = None
    connected: bool | None = None
    digest: str | None = None
    changed_at: float | None = None
    pull_at: float | None = None
    retry_at: float = 0
    errors: int = 0
    busy: str | None = None
    last_success: str | None = None
    last_error: str | None = None
    conflict: dict | None = None
    epoch: int = 0
    cancelled: Event = field(default_factory=Event)


class AutoSyncManager:
    def __init__(self, data_dir, *, adapter=None, clock=time.monotonic,
                 debounce=120, pull_interval=900, poll_interval=15):
        if min(debounce, pull_interval, poll_interval) <= 0:
            raise ValueError('Scheduling intervals must be positive')
        self.data_dir = absolute(data_dir)
        self._path = self.data_dir / 'skills-autosync'
        self._adapter = adapter if adapter is not None else BackupAdapter(self.data_dir)
        self._clock = clock
        self.debounce, self.pull_interval, self.poll_interval = debounce, pull_interval, poll_interval
        self._mutex = RLock()
        self._step = Lock()
        self._wake = Event()
        self._stop = Event()
        self._worker = None
        self._states = {agent: self._load(agent) for agent in AGENTS}

    def _load(self, agent):
        path = self._path / (agent + '.json')
        try:
            st = plain(path, file=True, missing=True)
            if st is None:
                return _State()
            if st.st_size > 4096 or st.st_nlink != 1:
                raise ValueError('Invalid autosync configuration')
            obj = json.loads(path.read_text(encoding='utf-8'))
            token, paused = obj['connection'], obj['pause']
            if (set(obj) != {'connection', 'pause'} or (token is not None and
                    (not isinstance(token, str) or not token or len(token) > 128)) or
                    (paused is not None and (not isinstance(paused, str) or paused not in _PAUSE))):
                raise ValueError('Invalid autosync configuration')
            return _State(connection=token, pause=paused)
        except (OSError, ValueError, TypeError, KeyError):
            return _State(last_error='invalid_autosync_config')

    def _save(self, agent, connection, pause):
        plain(self._path, missing=True)
        self._path.mkdir(parents=True, exist_ok=True)
        path = self._path / (agent + '.json')
        st = plain(path, file=True, missing=True)
        if st is not None and st.st_nlink != 1:
            raise SkillError('unsafe_path', 'Autosync settings must not be hard-linked')
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self._path, delete=False) as out:
                pending = Path(out.name)
                json.dump({'connection': connection, 'pause': pause}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(pending, path)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def _get(self, agent):
        if agent not in AGENTS:
            raise SkillError('invalid_agent', 'Choose Codex or Hermes')
        return self._states[agent]

    def status(self, agent):
        with self._mutex:
            s = self._get(agent)
            now = self._clock()
            if s.busy:
                phase = 'stopping' if s.cancelled.is_set() else s.busy
            elif s.connection is None:
                phase = 'disabled'
            elif s.pause:
                phase = 'paused_conflict' if s.pause not in ('deployments_active', 'concurrent_changes') else 'paused'
            elif self._stop.is_set():
                phase = 'stopped'
            elif s.retry_at > now:
                phase = 'retry_wait'
            elif s.changed_at is not None:
                phase = 'debouncing'
            else:
                phase = 'idle'
            due = (None if s.connection is None or s.pause or s.busy or self._stop.is_set() else
                   max(s.retry_at, s.changed_at + self.debounce) if s.changed_at is not None else
                   max(s.retry_at, s.pull_at or now))
            return {'agent': agent, 'enabled': s.connection is not None, 'connected': s.connected,
                    'state': phase, 'paused': s.pause is not None, 'pause_reason': s.pause,
                    'last_success_at': s.last_success, 'last_error': s.last_error,
                    'next_in_seconds': None if due is None else max(0, round(due - now)),
                    'conflict': deepcopy(s.conflict)}

    def set_enabled(self, agent, enabled, *, confirm=False):
        if type(enabled) is not bool:
            raise SkillError('invalid_payload', 'enabled must be a boolean')
        if enabled and confirm is not True:
            raise SkillError('confirmation_required', 'Confirm automatic sync for this agent')
        with self._mutex:
            s = self._get(agent)
            if not enabled:
                s.epoch += 1
                s.cancelled.set()
                # Runtime stop is immediate even if the settings write fails.
                s.connection, s.pause, s.conflict = None, None, None
                s.changed_at, s.pull_at, s.retry_at = None, None, 0
                s.last_error, s.errors = None, 0
                self._save(agent, None, None)
                self._wake.set()
                return self.status(agent)
            if s.busy or self._stop.is_set():
                raise SkillError('autosync_busy', 'Wait for the previous sync or restart the worker')
            before, epoch = s, s.epoch
        observation = self._adapter.observe(agent)
        with self._mutex:
            s = self._get(agent)
            if s is not before or s.epoch != epoch or self._stop.is_set():
                raise SkillError('autosync_busy', 'Automatic sync changed while checking the connection')
            s.connected = observation.connection is not None
            if not observation.connection:
                raise SkillError('sync_not_connected', 'Connect a repository before enabling automatic sync')
            if s.connection == observation.connection:
                return self.status(agent)  # An enable request cannot silently clear a conflict.
            self._save(agent, observation.connection, None)
            self._states[agent] = _State(connection=observation.connection, connected=True,
                                         digest=observation.digest,
                                         changed_at=self._clock() if observation.dirty else None,
                                         pull_at=self._clock() + self.pull_interval)
            self._wake.set()
            return self.status(agent)

    def resume(self, agent, *, confirm=False):
        if confirm is not True:
            raise SkillError('confirmation_required', 'Confirm resuming automatic sync')
        with self._mutex:
            s = self._get(agent)
            if not s.connection or not s.pause or s.busy or self._stop.is_set():
                raise SkillError('invalid_payload', 'There is no paused automatic sync to resume')
            self._save(agent, s.connection, None)
            s.pause, s.conflict, s.last_error = None, None, None
            s.changed_at, s.retry_at, s.errors = None, 0, 0
            s.pull_at = self._clock()
            self._wake.set()
            return self.status(agent)

    def action(self, agent, op, payload=None):
        payload = {} if payload is None else payload
        try:
            if not isinstance(payload, dict):
                raise SkillError('invalid_payload', 'Expected an options object')
            if op == 'autosync_status' and not payload:
                status = self.status(agent)
            elif op == 'autosync_set' and 'enabled' in payload and not set(payload) - {'enabled', 'confirm'}:
                status = self.set_enabled(agent, payload['enabled'], confirm=payload.get('confirm'))
            elif op == 'autosync_resume' and not set(payload) - {'confirm'}:
                status = self.resume(agent, confirm=payload.get('confirm'))
            else:
                raise SkillError('invalid_payload', 'Invalid automatic sync action')
            return {'ok': True, 'status': status}
        except (SkillError, backup._Error) as exc:
            return _failure(exc.code, 'Automatic sync settings could not be applied' if isinstance(exc, backup._Error)
                            else str(exc))
        except (OSError, ValueError, sqlite3.Error):
            return _failure('autosync_io', 'Automatic sync settings could not be applied')

    def _invalidate(self, agent, s, *, connected):
        s.epoch += 1
        s.cancelled.set()
        s.connection, s.pause, s.connected = None, None, connected
        s.changed_at, s.pull_at, s.retry_at = None, None, 0
        s.last_error = 'connection_changed' if connected else 'sync_not_connected'
        try:
            self._save(agent, None, None)
        except (OSError, ValueError):
            s.last_error = 'autosync_io'

    def _failed(self, agent, s, code, conflict=None):
        if code in _PAUSE:
            s.pause = code
            s.conflict = deepcopy(conflict) if isinstance(conflict, dict) else None
            try:
                self._save(agent, s.connection, code)
            except (OSError, ValueError):
                # Keep the in-memory pause. Never fall back to transferring again.
                s.last_error = 'autosync_io'
                return
        elif code == 'remote_ahead':
            s.changed_at, s.pull_at = None, self._clock()
        elif code == 'library_changed':
            s.changed_at = self._clock()
        else:
            s.errors += 1
            s.retry_at = self._clock() + min(3600, self.debounce * 2 ** min(5, s.errors - 1))
        s.last_error = code

    def _tick_agent(self, agent):
        with self._mutex:
            s = self._states[agent]
            if not s.connection or s.pause or s.busy or s.cancelled.is_set() or self._stop.is_set():
                return
            if s.retry_at > self._clock():
                return
            event = s.cancelled
        try:
            observed = self._adapter.observe(agent)
        except (OSError, ValueError, sqlite3.Error, backup._Error) as exc:
            with self._mutex:
                if not event.is_set() and s is self._states[agent]:
                    self._failed(agent, s, getattr(exc, 'code', 'autosync_io'))
            return
        with self._mutex:
            if event.is_set() or s is not self._states[agent] or self._stop.is_set():
                return
            if s.connection != observed.connection:
                self._invalidate(agent, s, connected=bool(observed.connection))
                return
            s.connected = True
            now = self._clock()
            if s.digest != observed.digest:
                s.digest = observed.digest
                s.changed_at = now if observed.dirty else None
            if s.pull_at is None:
                s.pull_at = now + self.pull_interval
            if observed.dirty and s.last_error != 'remote_ahead':
                if s.changed_at is None:
                    s.changed_at = now
                if now - s.changed_at < self.debounce:
                    return
                op, s.busy = 'sync_push', 'pushing'
            else:
                s.changed_at = None
                if s.pull_at > now:
                    return
                op, s.busy = 'sync_pull', 'pulling'
        try:
            result = self._adapter.transfer(agent, op, observed, event)
        except (OSError, ValueError, sqlite3.Error, backup._Error) as exc:
            result = _failure(getattr(exc, 'code', 'autosync_io'), 'Automatic transfer failed')
        finally:
            with self._mutex:
                s.busy = None
        with self._mutex:
            if event.is_set() or s is not self._states[agent] or self._stop.is_set():
                return
            if result.get('ok') is True:
                s.last_success, s.last_error = _stamp(), None
                s.errors, s.retry_at, s.changed_at = 0, 0, None
                s.pull_at = self._clock() + self.pull_interval
            else:
                error = result.get('error')
                code = result.get('code') or (error.get('code') if isinstance(error, dict) else None) or 'sync_failed'
                if code == 'connection_changed':
                    self._invalidate(agent, s, connected=True)
                else:
                    self._failed(agent, s, code, result.get('conflict'))

    def tick(self):
        """Run one polling pass; deterministic when used with an injected clock."""
        if not self._step.acquire(blocking=False):
            return
        try:
            for agent in AGENTS:
                self._tick_agent(agent)
        finally:
            self._step.release()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.tick()
            self._wake.wait(self.poll_interval)

    def start(self):
        """Start once; the desktop owns when this is called."""
        with self._mutex:
            if self._worker is not None and self._worker.is_alive():
                return
            if self._stop.is_set():
                self._stop.clear()
                for s in self._states.values():
                    s.cancelled = Event()
            self._worker = Thread(target=self._loop, name='skills-autosync', daemon=True)
            self._worker.start()

    def stop(self, *, wait=0):
        """Stop scheduling immediately. A bounded wait is optional for tests."""
        with self._mutex:
            self._stop.set()
            for s in self._states.values():
                s.epoch += 1
                s.cancelled.set()
            self._wake.set()
            worker = self._worker
        if wait and worker is not None and worker is not current_thread():
            worker.join(wait)
        return worker is None or not worker.is_alive()


def get_autosync(data_dir):
    """Share one desktop worker per data directory inside this process."""
    path = absolute(data_dir)
    key = os.path.normcase(str(path))
    with _registry_lock:
        instance = _registry.get(key)
        if instance is None:
            instance = AutoSyncManager(path)
            _registry[key] = instance
        return instance
