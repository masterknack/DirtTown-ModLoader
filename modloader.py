"""DirtTown offline mod overlay launcher. Python 3.11+."""
import shutil, argparse, logging, os, re, struct, tempfile, tomllib, sys, hashlib, json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from pack_digp import encode, validate, write_payload, build as pack_directory
from unpack_digp import entries, decode, payload_chunks
from monitor import DEFAULT_STARTUP_TIMEOUT, matching_process_state, supervise, snapshot

LOG = logging.getLogger('modloader')
VERSION = '0.7.1'
MOD_DIR = 'modloader'
BACKUP = 'data.pack.original'
STAGED = 'data.pack.staged'
STATE = 'recovery-state.json'
SESSION = 'modloader-session.json'
LEGACY_BACKUP = 'data.pack.modloader-original'
LEGACY_STAGED = 'data.pack.modloader-staged'
LEGACY_STATE = 'data.pack.modloader-state.json'
LEGACY_LOCK = 'modloader.lock'

def mod_dir(root):
    path = root / MOD_DIR
    if path.is_symlink(): raise ValueError('modloader directory must not be a symlink')
    path.mkdir(exist_ok=True)
    return path

def _safe_dir(path):
    if path.is_symlink(): raise ValueError(f'ModLoader directory must not be a symlink: {path}')
    path.mkdir(exist_ok=True)
    return path

def _unique_path(directory, name):
    path = directory / name
    if not path.exists() and not path.is_symlink(): return path
    candidate = Path(name)
    for index in range(1, 10000):
        path = directory / f'{candidate.stem}-{index}{candidate.suffix}'
        if not path.exists() and not path.is_symlink(): return path
    raise RuntimeError(f'Could not create a unique archive name in {directory}')

def _log_archive_path(logs, timestamp):
    day = _safe_dir(logs / timestamp.strftime('%Y-%m-%d'))
    stem = timestamp.strftime('%H-%M-%S-%f')
    return _unique_path(day, f'{stem}.log')

def _demote_latest_logs(logs):
    for path in sorted(logs.rglob('*.latest.log')):
        if path.is_symlink(): raise ValueError(f'Log file must not be a symlink: {path}')
        if not path.is_file(): continue
        target = _unique_path(path.parent, path.name[:-len('.latest.log')] + '.log')
        os.replace(path, target)

def _prune_logs(logs, keep=None, limit=5):
    files = []
    for path in logs.rglob('*.log'):
        if path.is_symlink(): raise ValueError(f'Log file must not be a symlink: {path}')
        if path.is_file(): files.append(path)
    files.sort(key=lambda path: (path.stat().st_mtime_ns, str(path)), reverse=True)
    files = [path for path in files if path != keep]
    keep_count = limit - 1 if keep is not None else limit
    for path in files[keep_count:]: path.unlink()

def setup_log_file(root):
    work = mod_dir(root)
    logs = _safe_dir(work / 'logs')
    for directory in logs.rglob('*'):
        if directory.is_symlink(): raise ValueError(f'Log directory must not be a symlink: {directory}')
    _demote_latest_logs(logs)

    # Preserve logs from versions that wrote directly beside the game EXE.
    legacy = root / 'modloader.log'
    if legacy.is_symlink(): raise ValueError('Legacy modloader.log must be a regular file')
    if legacy.exists():
        if not legacy.is_file(): raise ValueError('Legacy modloader.log must be a regular file')
        stamp = datetime.fromtimestamp(legacy.stat().st_mtime)
        os.replace(legacy, _log_archive_path(logs, stamp))

    now = datetime.now()
    day = _safe_dir(logs / now.strftime('%Y-%m-%d'))
    stem = now.strftime('%H-%M-%S-%f')
    latest = day / f'{stem}.latest.log'
    suffix = 1
    while latest.exists():
        latest = day / f'{stem}-{suffix}.latest.log'
        suffix += 1
    _prune_logs(logs, latest)
    handler = logging.FileHandler(latest, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    return handler

def migrate_legacy_session(root):
    work = mod_dir(root)
    legacy = root / SESSION
    if legacy.is_symlink(): raise ValueError('Legacy session report must be a regular file')
    if not legacy.exists(): return
    if not legacy.is_file(): raise ValueError('Legacy session report must be a regular file')
    destination = work / SESSION
    if destination.is_symlink(): raise ValueError('modloader session report must not be a symlink')
    if destination.exists():
        if not destination.is_file(): raise ValueError('modloader session report must be a regular file')
        archive = _safe_dir(work / 'legacy')
        stamp = datetime.fromtimestamp(destination.stat().st_mtime).strftime('%Y%m%d-%H%M%S-%f')
        destination = _unique_path(archive, f'modloader-session-{stamp}.json')
        os.replace(work / SESSION, destination)
    os.replace(legacy, work / SESSION)

def recovery_paths(root, legacy=False):
    if legacy:
        return root / 'data.pack', root / LEGACY_BACKUP, root / LEGACY_STAGED, root / LEGACY_STATE
    work = mod_dir(root)
    return root / 'data.pack', work / BACKUP, work / STAGED, work / STATE

def has_recovery_artifacts(root):
    new = recovery_paths(root)
    legacy = recovery_paths(root, legacy=True)
    new_found = any(path.exists() or path.is_symlink() for path in new[1:])
    legacy_found = any(path.exists() or path.is_symlink() for path in legacy[1:])
    if new_found and legacy_found:
        raise RuntimeError('Recovery files exist in both root and modloader/; inspect them before continuing')
    return new_found or legacy_found

def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()

@contextmanager
def lock(root):
    work = mod_dir(root)
    current_path = work / 'modloader.lock'
    legacy_path = root / LEGACY_LOCK
    if current_path.is_symlink(): raise ValueError('modloader/modloader.lock must not be a symlink')
    if legacy_path.is_symlink(): raise ValueError('Legacy modloader.lock must not be a symlink')
    files = []
    legacy_locked = False

    def acquire(path):
        f = path.open('a+b')
        files.append(f)
        f.seek(0, 2)
        if f.tell() == 0: f.write(b'0'); f.flush()
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f

    try:
        if legacy_path.exists():
            if legacy_path.is_symlink(): raise ValueError('Legacy modloader.lock must not be a symlink')
            acquire(legacy_path)
            legacy_locked = True
        acquire(current_path)
        yield
    finally:
        for f in reversed(files):
            f.close()
        if legacy_locked and legacy_path.exists():
            archive = _safe_dir(work / 'legacy')
            moved = archive / f'modloader.lock-{datetime.now():%Y%m%d-%H%M%S-%f}'
            try:
                os.replace(legacy_path, moved)
            except OSError as e:
                LOG.warning('Could not move legacy lock file: %s', e)


def mods(root, names):
    folder = root / 'mods'
    folder.mkdir(exist_ok=True)
    if folder.is_symlink(): raise ValueError('mods must not be a symlink')
    found = []
    for mod in sorted(folder.iterdir()):
        if not mod.is_dir() or mod.is_symlink(): continue
        manifest = mod / 'mod.toml'
        if manifest.is_symlink(): raise ValueError(f'Symlink in mod: {manifest}')
        config = tomllib.loads(manifest.read_text(encoding='utf-8-sig')).get('mod', {}) if manifest.is_file() else {}
        if not isinstance(config, dict): raise ValueError(f'Invalid [mod] table: {mod.name}')
        enabled = config.get('enabled', True)
        if type(enabled) is not bool: raise ValueError(f'Invalid enabled value: {mod.name}; expected true or false')
        if not enabled:
            LOG.info('Mod disabled: %s', mod.name); continue
        priority = config.get('priority', 0)
        if type(priority) is not int: raise ValueError(f'Invalid priority: {mod.name}')
        found.append((priority, mod.name, mod))
        LOG.info("Mod detected: %s (priority=%s)", mod.name, priority)
    overlay = {}
    for _, name, mod in sorted(found):
        for p in sorted(mod.rglob('*')):
            if p.is_symlink(): raise ValueError(f'Symlink in mod: {p}')
            if not p.is_file() or p == mod / 'mod.toml': continue
            rel = p.relative_to(mod).as_posix(); validate(rel)
            if rel not in names: raise ValueError(f'Unknown resource: {name}/{rel}')
            if rel in overlay: LOG.warning('Conflict: %s: %s -> %s', rel, overlay[rel][0], name)
            if rel.lower().endswith('.toml'): tomllib.loads(p.read_text(encoding='utf-8-sig'))
            overlay[rel] = (name, p)
    for _, name, _ in found:
        count = sum(owner == name for owner, _ in overlay.values())
        if count: LOG.info('Mod prepared: %s (%d effective overrides)', name, count)
        else: LOG.warning('Mod has no effective overrides: %s', name)
    return overlay

def build(root, output, overlay=None):
    original = root / 'data.pack'
    if output.resolve() == original.resolve(): raise ValueError('Refusing to overwrite original')
    with original.open('rb') as src:
        records = list(entries(src))
        if overlay is None: overlay = mods(root, {r[0] for r in records})
        src.seek(4); _, index_offset, _ = struct.unpack('<IQQ', decode(src.read(20),4))
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as dst:
                tmp = Path(dst.name); src.seek(0); remaining = index_offset
                while remaining:
                    chunk = src.read(min(remaining, 1024*1024))
                    if not chunk: raise ValueError('Truncated archive')
                    dst.write(chunk); remaining -= len(chunk)
                index = bytearray(struct.pack('<I',len(records)))
                for name, flag, pos, plain, stored in records:
                    if name in overlay:
                        owner, path = overlay[name]
                        dst.write(bytes((-dst.tell())%32)); pos = dst.tell()
                        with path.open('rb') as data:
                            plain, stored = write_payload(data, dst, pos, bool(flag))
                        LOG.info('Override: %s (%s)', name, owner)
                    b = validate(name); index.extend(struct.pack('<H',len(b))+b+struct.pack('<BQQQ',flag,pos,plain,stored))
                off = dst.tell(); dst.write(encode(index,off)); dst.seek(4)
                dst.write(encode(struct.pack('<IQQ',1,off,len(index)),4))
            with tmp.open('rb') as check: list(entries(check))
            os.replace(tmp,output)
        finally:
            if tmp is not None: tmp.unlink(missing_ok=True)
    LOG.info('Built %s (%d overrides)', output, len(overlay))
    return overlay

def recover(root):
    current = recovery_paths(root)
    previous = recovery_paths(root, legacy=True)
    current_found = any(path.exists() or path.is_symlink() for path in current[1:])
    previous_found = any(path.exists() or path.is_symlink() for path in previous[1:])
    if current_found and previous_found:
        raise RuntimeError('Recovery files exist in both root and modloader/; inspect them before continuing')
    original, backup, staged, state = previous if previous_found else current
    for path in (backup, staged, state):
        if path.is_symlink(): raise ValueError(f'Recovery file must not be a symlink: {path}')
    ensure_game_stopped(root)
    if not backup.exists():
        if state.exists():
            info = json.loads(state.read_text())
            if not original.exists() or digest(original) != info['original']:
                raise RuntimeError('Recovery state inconsistent; inspect archive manually')
            state.unlink()
        staged.unlink(missing_ok=True)
        return
    if not state.exists(): raise RuntimeError('Backup exists without recovery state; inspect manually')
    info = json.loads(state.read_text())
    if digest(backup) != info['original']: raise RuntimeError('Backup hash mismatch; refusing restore')
    if original.exists() and digest(original) != info['staged']:
        raise RuntimeError('data.pack changed externally; refusing to overwrite; backup preserved')
    os.replace(backup, original)
    state.unlink()
    staged.unlink(missing_ok=True)
    LOG.info('Original data.pack restored and verified')

def ensure_game_stopped(root):
    exe = root / 'dirttown.exe'
    if not exe.is_file():
        return
    running, unknown = matching_process_state(exe)
    if running:
        raise RuntimeError(f'Dirt Town is already running from this folder (PID {", ".join(map(str, running))}); close it first')
    if unknown:
        raise RuntimeError(f'Cannot confirm whether Dirt Town is running (PID {", ".join(map(str, unknown))}); refusing to modify data.pack')

def find_steam_app_id(root, override=None):
    if override is not None:
        return str(override)
    steamapps = root.parent.parent
    for manifest in steamapps.glob('appmanifest_*.acf'):
        try:
            content = manifest.read_text(encoding='utf-8-sig')
        except OSError:
            continue
        install = re.search(r'^\s*"installdir"\s*"([^"]+)"', content, re.MULTILINE)
        appid = re.search(r'^\s*"appid"\s*"([0-9]+)"', content, re.MULTILINE)
        if install and appid and install.group(1).casefold() == root.name.casefold():
            return appid.group(1)
    raise RuntimeError(f'Steam app manifest not found for {root}; use --steam-appid to specify it')

def start_steam(app_id):
    if os.name != 'nt':
        raise RuntimeError('Steam URI launching is supported on Windows only')
    os.startfile(f'steam://run/{app_id}')

def launch(root, interval=2, heartbeat=30, startup_timeout=DEFAULT_STARTUP_TIMEOUT, app_id=None):
    original, backup, staged, state = recovery_paths(root)
    exe = root / 'dirttown.exe'
    if not exe.is_file(): raise FileNotFoundError(exe)
    app_id = find_steam_app_id(root, app_id)
    ensure_game_stopped(root)
    if has_recovery_artifacts(root):
        raise RuntimeError('Previous session artifacts found. Close game, then run --recover')
    with original.open('rb') as f: overlay = mods(root, {r[0] for r in entries(f)})
    initial = snapshot(root)
    if not overlay:
        LOG.warning('No active overrides; starting vanilla game through Steam (AppID %s)', app_id)
        ensure_game_stopped(root)
        start_steam(app_id)
        return supervise(None, root, initial, interval, heartbeat, game_executable=exe, startup_timeout=startup_timeout)
    build(root, staged, overlay)
    # Building can take time; close the race with a game started during that work.
    ensure_game_stopped(root)
    info = {'original': digest(original), 'staged': digest(staged)}
    with state.open('x') as f:
        json.dump(info, f); f.flush(); os.fsync(f.fileno())
    os.rename(original, backup)
    launch_attempted = False
    supervision_completed = False
    try:
        os.replace(staged, original)
        LOG.info('Mod archive activated; requesting Steam launch (AppID %s)', app_id)
        # Once ShellExecute is entered, an exception does not prove Steam did not start the game.
        launch_attempted = True
        start_steam(app_id)
        result = supervise(None, root, initial, interval, heartbeat, game_executable=exe, startup_timeout=startup_timeout)
        supervision_completed = True
        return result
    finally:
        if not launch_attempted or supervision_completed:
            recover(root)
        else:
            LOG.warning('Steam game may still be running. Backup preserved. Close the game before --recover.')

def archive_check(path, destination=None):
    with path.open('rb') as f:
        records = list(entries(f))
        seen = set()
        for name, flag, pos, plain, stored in records:
            validate(name)
            folded = name.casefold()
            if folded in seen or any(part.endswith((' ', '.')) or part.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))} for part in name.split('/')):
                raise ValueError(f'Unsafe Windows path: {name}')
            seen.add(folded)
            toml_data = bytearray() if name.lower().endswith('.toml') else None
            target = None
            if destination is not None:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target = target.open('xb')
            try:
                for chunk in payload_chunks(f, flag, pos, plain, stored):
                    if target is not None: target.write(chunk)
                    if toml_data is not None: toml_data.extend(chunk)
            finally:
                if target is not None: target.close()
            if toml_data is not None: tomllib.loads(toml_data.decode('utf-8-sig'))
        LOG.info('Archive validated: %s (%d files)', path, len(records))

def unpack(root):
    dest = root / 'data'
    if dest.exists(): raise FileExistsError('data already exists; rename it before unpacking')
    with tempfile.TemporaryDirectory(dir=mod_dir(root), prefix='.unpack-') as temp:
        stage = Path(temp) / 'data'; stage.mkdir()
        archive_check(root / 'data.pack', stage)
        stage.rename(dest)
    LOG.info('Extracted to %s', dest)

def validate_data(root):
    source = root / 'data'
    if not source.is_dir() or source.is_symlink(): raise ValueError('Missing or invalid data directory')
    count = 0
    for path in source.rglob('*'):
        if path.is_symlink(): raise ValueError(f'Symlink: {path}')
        if path.is_file() and path != source / 'data.pack':
            validate(path.relative_to(source).as_posix()); count += 1
            if path.suffix.lower() == '.toml': tomllib.loads(path.read_text(encoding='utf-8-sig'))
    if not count: raise ValueError('Empty data directory')
    LOG.info('Data directory validated: %d files', count)

def pack_game_data(root, no_backup=False):
    ensure_game_stopped(root)
    source = root / 'data'
    output = root / 'data.pack'
    backup = root / 'data.old'
    validate_data(root)
    if output.is_symlink(): raise ValueError('Refusing to replace a symlink at data.pack')
    if backup.is_symlink() and not no_backup: raise ValueError('Refusing to replace a symlink at data.old')
    if output.exists() and backup.exists() and not no_backup:
        raise FileExistsError(f'{backup} already exists; move it aside or use --no-backup')

    backup_created = False
    with tempfile.TemporaryDirectory(dir=mod_dir(root), prefix='.pack-') as temp:
        temp = Path(temp)
        candidate = temp / 'data.pack'
        pack_directory(source, candidate, exclude={'data.pack'})
        archive_check(candidate)
        ensure_game_stopped(root)
        staged_backup = None
        if output.exists() and not no_backup:
            staged_backup = temp / 'data.old'
            shutil.copy2(output, staged_backup)
            if digest(staged_backup) != digest(output):
                raise RuntimeError('Backup verification failed; original data.pack was preserved')
        # Recheck after staging the optional backup, immediately before replacing the live archive.
        ensure_game_stopped(root)
        if staged_backup is not None:
            os.replace(staged_backup, backup)
            backup_created = True
        try:
            # This final guard is directly adjacent to replacing data.pack.
            ensure_game_stopped(root)
        except Exception:
            if backup_created and backup.exists():
                try:
                    if output.exists() and digest(backup) == digest(output):
                        backup.unlink()
                        backup_created = False
                except OSError as cleanup_error:
                    LOG.warning('Could not remove the unused data.old copy after aborting pack: %s', cleanup_error)
            raise
        os.replace(candidate, output)
    LOG.info('Packed and validated: %s', output)
    if backup_created:
        LOG.info('Previous data.pack saved as %s', backup)


def main():
    default = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--game-dir', type=Path, default=default)
    g = p.add_mutually_exclusive_group()
    g.add_argument('--build-only', action='store_true')
    g.add_argument('--recover', action='store_true')
    g.add_argument('--unpack', action='store_true')
    g.add_argument('--pack', action='store_true')
    g.add_argument('--valid', '--vaild', dest='valid', action='store_true')
    p.add_argument('--force', action='store_true', help='Legacy flag; --pack now replaces the root data.pack by default')
    p.add_argument('--no-backup', action='store_true', help='Replace data.pack without saving the previous pack as data.old')
    p.add_argument('--no-pause', action='store_true', help='Legacy no-op; the loader now exits automatically')
    p.add_argument('--monitor-interval', type=float, default=2)
    p.add_argument('--heartbeat', type=float, default=30)
    p.add_argument('--startup-timeout', type=float, default=DEFAULT_STARTUP_TIMEOUT, help='Seconds to wait for Steam to start the game')
    p.add_argument('--steam-appid', type=int, help='Override AppID detection from the Steam app manifest')
    a = p.parse_args()
    import math
    if not all(math.isfinite(v) and v >= 0.1 for v in (a.monitor_interval, a.heartbeat, a.startup_timeout)): p.error('Monitoring and startup times must be finite and >= 0.1 seconds')
    if a.steam_appid is not None and a.steam_appid <= 0: p.error('--steam-appid must be a positive integer')
    if a.no_backup and not a.pack: p.error('--no-backup can only be used with --pack')
    root = a.game_dir.resolve()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S', handlers=[logging.StreamHandler(sys.stdout)])
    file_handler = None
    try:
        with lock(root):
            migrate_legacy_session(root)
            file_handler = setup_log_file(root)
            LOG.addHandler(file_handler)
            LOG.info('DirtTown ModLoader v%s | %s', VERSION, root)
            if a.recover: recover(root)
            elif a.unpack or a.pack or a.valid:
                if has_recovery_artifacts(root): raise RuntimeError('Recover previous session first')
                if a.unpack: unpack(root)
                elif a.pack:
                    if a.force: LOG.warning('--force is deprecated; --pack now replaces root data.pack by default')
                    pack_game_data(root, a.no_backup)
                else:
                    archive_check(root / 'data.pack')
                    if (root / 'data').is_dir():
                        validate_data(root)
            elif a.build_only:
                if has_recovery_artifacts(root): raise RuntimeError('Recover previous session first')
                build(root, mod_dir(root) / 'data.modded.pack')
            else: return launch(root, a.monitor_interval, a.heartbeat, a.startup_timeout, a.steam_appid)
        return 0
    except KeyboardInterrupt:
        LOG.warning('Interrupted'); return 130
    except Exception:
        LOG.exception('ModLoader failed'); return 1
    finally:
        if file_handler is not None:
            LOG.removeHandler(file_handler)
            file_handler.close()
if __name__ == '__main__': raise SystemExit(main())
