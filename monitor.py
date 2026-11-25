"""Read-only runtime supervision; no injection or live archive modification."""
import ctypes
import logging, time, json, tomllib, os
from pathlib import Path
LOG = logging.getLogger('modloader')
# Steam may take a while to start the game after accepting the URI request.
DEFAULT_STARTUP_TIMEOUT = 60
PROCESS_TREE_QUIESCENCE = 0.5

def _windows_processes():
    """Return (pid -> (parent pid, executable name)) from Toolhelp."""
    if os.name != 'nt':
        return None
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long),
            ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid = ctypes.c_void_p(-1).value
    if snapshot == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    result = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result

def _image_path(pid):
    if os.name != 'nt':
        return None
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)

def matching_process_state(executable):
    """Return exact matches and same-name PIDs whose image path is unreadable."""
    table = _windows_processes()
    if table is None:
        return [], []
    wanted = os.path.normcase(str(Path(executable).resolve())).casefold()
    matches = []
    unknown = []
    for pid, (_, image) in table.items():
        if image.casefold() != Path(executable).name.casefold():
            continue
        actual = _image_path(pid)
        if not actual:
            unknown.append(pid)
            continue
        try:
            actual_path = os.path.normcase(str(Path(actual).resolve())).casefold()
        except OSError:
            unknown.append(pid)
            continue
        if actual_path == wanted: matches.append(pid)
    return matches, unknown

def matching_processes(executable):
    """Find running processes whose full image path matches executable."""
    return matching_process_state(executable)[0]

class ProcessTree:
    """Keep handles to tracked roots and descendants observed on Windows."""
    def __init__(self, root_pid=None):
        self.root_pid = root_pid
        self.roots = set()
        self.observed = set()
        self.handles = {}
        self.exit_codes = {}
        self.unknown = set()
        self.kernel32 = None
        if root_pid is not None:
            self.add_root(root_pid)
        if os.name == 'nt':
            from ctypes import wintypes
            self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel32.OpenProcess.restype = wintypes.HANDLE
            self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def add_root(self, pid):
        self.roots.add(pid)
        self.observed.add(pid)

    def active(self, root_running):
        self.unknown.clear()
        if self.kernel32 is None:
            return [self.root_pid] if root_running and self.root_pid is not None else []
        processes = _windows_processes()
        descendants = set(self.observed)
        changed = True
        while changed:
            changed = False
            for pid, (parent, _) in processes.items():
                if parent in descendants and pid not in descendants:
                    descendants.add(pid)
                    changed = True
        self.observed.update(descendants)
        if root_running and self.root_pid is not None:
            descendants.add(self.root_pid)
        for pid in descendants:
            if pid not in self.handles:
                if pid not in processes and not (root_running and pid == self.root_pid):
                    continue
                handle = self.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                if handle:
                    self.handles[pid] = handle
                else:
                    self.unknown.add(pid)
        active = []
        for pid, handle in list(self.handles.items()):
            wait_result = self.kernel32.WaitForSingleObject(handle, 0)
            if wait_result == 0x00000102:  # WAIT_TIMEOUT
                active.append(pid)
            elif wait_result == 0x00000000:  # WAIT_OBJECT_0: confirmed exited
                from ctypes import wintypes
                code = wintypes.DWORD()
                if self.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    self.exit_codes[pid] = code.value
                self.kernel32.CloseHandle(handle)
                del self.handles[pid]
            else:
                # WAIT_FAILED and unexpected results are not proof of process exit.
                self.unknown.add(pid)
        if root_running and self.root_pid is not None and self.root_pid not in active:
            active.append(self.root_pid)
        return active

    def close(self):
        if self.kernel32 is not None:
            for handle in self.handles.values():
                self.kernel32.CloseHandle(handle)
        self.handles.clear()

def snapshot(root):
    result = {}
    for p in sorted((root/'mods').rglob('*')):
        if p.is_symlink():
            result[str(p.relative_to(root))] = ('symlink',)
        elif p.is_file():
            try:
                s = p.stat(); result[str(p.relative_to(root))] = (s.st_size,s.st_mtime_ns)
            except OSError as e: LOG.warning('Cannot inspect %s: %s',p,e)
    return result

def stamp(p):
    try:
        s=p.stat(); return s.st_size,s.st_mtime_ns,s.st_ino
    except OSError: return None

def supervise(proc, root, initial, interval=2, heartbeat=30, game_executable=None, startup_timeout=DEFAULT_STARTUP_TIMEOUT):
    start=time.monotonic(); last=start; next_scan=start; changes=0; errors=0
    archive=root/'data.pack'; baseline=stamp(archive); previous=baseline
    report={'pid':proc.pid if proc else None,'game_pid':None,'started':time.strftime('%Y-%m-%dT%H:%M:%S'),'status':'waiting_for_game' if game_executable else 'running','mod_changes':0,'validation_errors':0}
    path=root/'modloader'/'modloader-session.json'
    tree=ProcessTree(proc.pid if proc else None); active=[]
    game_pids=[]
    empty_since=None
    startup_deadline=start+startup_timeout if game_executable else None
    startup_timeout_logged=False; uncertainty_logged=False
    def save():
        try:
            if path.parent.is_symlink(): raise ValueError('modloader directory must not be a symlink')
            path.parent.mkdir(exist_ok=True)
            tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(report,indent=2),encoding='utf-8'); os.replace(tmp,path)
        except (OSError,ValueError) as e: LOG.warning('Cannot write session report: %s',e)
    save()
    if game_executable:
        LOG.info('Waiting up to %.1fs for Steam game process %s',startup_timeout,game_executable)
    LOG.info('Runtime monitor active (interval %.1fs)',interval)
    try:
        while True:
            root_running=proc is not None and proc.poll() is None
            path_unknown=[]
            if game_executable:
                matching, path_unknown = matching_process_state(game_executable)
                for pid in matching:
                    if pid not in game_pids:
                        game_pids.append(pid)
                        tree.add_root(pid)
                        report.update(game_pid=pid,status='running')
                        LOG.info('Steam game process detected (PID %d)',pid)
            active=tree.active(root_running)
            unknown_pids=sorted(set(tree.unknown) | set(path_unknown))
            if active:
                empty_since=None
            if active and report.get('status') == 'monitor_uncertain':
                report.update(status='running')
                report.pop('unobserved_pids', None)
                uncertainty_logged=False
            elif not tree.unknown and report.get('status') == 'monitor_uncertain':
                report.pop('unobserved_pids', None)
            if not active:
                if unknown_pids:
                    now=time.monotonic()
                    if not uncertainty_logged:
                        LOG.error('Cannot confirm whether tracked process(es) are still running: %s; keeping the active archive', ', '.join(map(str, unknown_pids)))
                        uncertainty_logged=True
                    report.update(status='monitor_uncertain',unobserved_pids=unknown_pids,uptime_seconds=round(now-start,1))
                    if now>=next_scan:
                        save(); next_scan=now+interval
                    time.sleep(min(0.25,interval))
                    continue
                uncertainty_logged=False
                if game_executable and not game_pids:
                    now=time.monotonic()
                    if now < startup_deadline:
                        if not startup_timeout_logged:
                            LOG.info('Launcher returned; waiting for Steam to start the game')
                            startup_timeout_logged=True
                        report.update(status='waiting_for_game',uptime_seconds=round(now-start,1)); save()
                        time.sleep(min(0.25,max(0.02,startup_deadline-now)))
                        continue
                    LOG.error('No game process appeared within %.1fs; restoring the original archive',startup_timeout)
                    report.update(status='startup_timeout',startup_timeout_seconds=startup_timeout)
                    break
                if proc is not None or game_pids:
                    now=time.monotonic()
                    if empty_since is None: empty_since=now
                    if now-empty_since < PROCESS_TREE_QUIESCENCE:
                        time.sleep(0.05)
                        continue
                break
            now=time.monotonic()
            if now>=next_scan:
                current=snapshot(root)
                for name in sorted(initial.keys() | current.keys()):
                    if initial.get(name)==current.get(name): continue
                    changes+=1; LOG.warning('Mod file %s: %s; restart required to apply', 'removed' if name not in current else 'added' if name not in initial else 'changed',name)
                    if name in current and name.lower().endswith('.toml'):
                        try:
                            if current[name]==('symlink',): raise ValueError('Symlink not allowed')
                            tomllib.loads((root/name).read_text(encoding='utf-8-sig'))
                            LOG.info('TOML syntax OK: %s',name)
                        except Exception as e:
                            errors+=1; LOG.error('Invalid mod file %s: %s',name,e)
                initial=current
                nowstamp=stamp(archive)
                if nowstamp!=previous:
                    LOG.error('Active archive metadata changed or archive missing; do not edit while playing'); errors+=1; previous=nowstamp
                if now-last>=heartbeat:
                    pid=game_pids[0] if game_pids else (proc.pid if proc else 0)
                    LOG.info('Game process tree active | PID %d | uptime %.0fs | mod changes %d | validation errors %d',pid,now-start,changes,errors); last=now
                report.update(mod_changes=changes,validation_errors=errors,uptime_seconds=round(now-start,1)); save()
                next_scan=now+interval
            if root_running:
                try: proc.wait(timeout=min(0.25, max(0.05, next_scan-time.monotonic())))
                except __import__('subprocess').TimeoutExpired: pass
            else:
                time.sleep(min(0.1, max(0.02, next_scan-time.monotonic())))
        if report.get('status') == 'startup_timeout':
            report.update(exit_code=1)
            return 1
        if proc:
            rc=proc.wait()
        elif game_pids and game_pids[-1] in tree.exit_codes:
            rc=tree.exit_codes[game_pids[-1]]
        elif game_pids:
            rc=1
            LOG.error('Game process ended without an observable exit code; reporting a conservative failure')
            report['exit_code_observed']=False
        else:
            rc=1
        LOG.log(logging.INFO if rc==0 else logging.ERROR,'Game exited: %d (0x%08X)',rc,rc & 0xffffffff)
        report.update(status='exited' if rc==0 else 'nonzero_exit',exit_code=rc)
        return rc
    finally:
        if active: report['status']='monitor_interrupted_game_running'
        report.update(ended=time.strftime('%Y-%m-%dT%H:%M:%S'),uptime_seconds=round(time.monotonic()-start,1)); save()
        tree.close()
