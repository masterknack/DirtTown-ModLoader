# DirtTown ModLoader v0.7.1 (Current release)

Windows; Python 3.11+ is required to build. `build_exe.bat` creates `dist/v0.7.1/modloader_dt.exe` and copies the active executable to the game folder as `modloader_dt.exe`. Store previous executables in `modloader/versions/`. To build manually from this directory:

```powershell
py -3 -m pip install pyinstaller
py -3 -m PyInstaller --noconfirm --clean --onefile --console --name modloader_dt --distpath dist/v0.7.1 --workpath build/v0.7.1 --specpath build/v0.7.1 modloader.py
Copy-Item dist/v0.7.1/modloader_dt.exe ../modloader_dt.exe
```

Python is not needed to run the EXE. The loader exits automatically after the game closes; it does not wait for Enter. The session report, lock, recovery files, and working files are stored in `modloader/`. Logs are stored in `modloader/logs/YYYY-MM-DD/`; at most five are kept, and the current log ends with `.latest.log`. A legacy root-level `modloader.log` and session report are migrated to the new layout.

## Create a mod

Copy a resource from the unpacked `data/` folder into `mods/`, keeping the same relative path. For example, copy `data/machines/truck.toml` to `mods/BetterTruck/machines/truck.toml` and edit the copy. The loader reads overrides from `mods/`; `data/` is only a reference unless you explicitly run `--pack`.

Optional `mods/BetterTruck/mod.toml`:

```toml
[mod]
enabled = true
priority = 100
```

`priority` must be a TOML integer from `-9223372036854775808` to `9223372036854775807`; the loader adds no narrower limit. If omitted, it defaults to `0`. Priority only matters when mods override the same resource: the higher number wins (for example, `100` overrides `0`), and negative numbers are allowed. For equal priorities, the mod folder name that sorts last alphabetically wins. Unknown resources and invalid TOML stop the launch. Check a mod build with `modloader_dt.exe --build-only`; it creates `modloader/data.modded.pack` without replacing `data.pack`. Then run `modloader_dt.exe` to launch the modded game through Steam.

Close the game before running the loader. It activates the modded archive and then starts the game through Steam. The AppID is read from the Steam manifest; use `--steam-appid` to specify it manually. The loader waits up to 60 seconds by default for the game process and keeps the modded archive active until that session ends. Use `--startup-timeout 120` to wait longer. The loader does not copy the game or modify the unpacked `data/` directory. It needs free space for an additional archive. It does not use a DLL hook or provide hot reload. A “mod prepared” log entry does not prove that the game engine loaded the overridden resource.

After a power failure or forced loader shutdown, close the game and run:

```powershell
.\modloader_dt.exe --recover
```

Do not delete recovery files from `modloader/` or legacy `data.pack.modloader-*` files from the game folder. If archive hashes conflict, the loader refuses to overwrite data. Do not update the game through Steam during a modded session or run multiple game instances. Keep a separate backup of the archive and saves. The loader monitors the game process by its full path, including when Steam starts it outside the loader's process tree.

`--build-only` creates `modloader/data.modded.pack` without starting the game. `--no-pause` is retained for compatibility and has no effect because the loader exits automatically.

## CLI

```powershell
.\modloader_dt.exe --unpack
.\modloader_dt.exe --pack
.\modloader_dt.exe --pack --no-backup
.\modloader_dt.exe --valid
.\modloader_dt.exe --vaild
```

### Unpack and repack `data.pack`

Run these commands from the game folder. `--unpack` refuses to overwrite an existing `data/`:

```powershell
.\modloader_dt.exe --unpack
# Edit files under data/...
.\modloader_dt.exe --pack
# Or skip the optional data.old backup:
.\modloader_dt.exe --pack --no-backup
.\modloader_dt.exe --valid
```

`--pack` writes the new archive to `./data.pack`; by default it saves the previous archive as `./data.old`. Close the game before packing.

`--unpack`: extracts `data.pack` to `data/` and refuses to overwrite an existing directory.

`--pack`: packs `data/` to `./data.pack`. By default, the existing archive is copied to `./data.old` before replacement. The command refuses to overwrite an existing `data.old`. `--no-backup` skips creating `data.old`. The legacy `--force` flag is accepted but is no longer needed.

`--valid` / `--vaild`: checks the main `./data.pack`, payload sizes, DEFLATE streams, paths, and TOML syntax. If `data/` exists, its files are checked too. This does not confirm configuration semantics or compatibility with the game engine. DIGP does not contain resource checksums.

Round-trip check: the original 393 files were unpacked and repacked with an identical SHA-256. Recovery after process exit was checked with a replacement test process, not with the Windows game.

## Monitoring

`--monitor-interval 2 --heartbeat 30` configures the monitoring interval and heartbeat in seconds. Mod changes are detected by file size and modification time; changed TOML files are checked for valid syntax. Changes require a game restart. Archive metadata changes are reported but not repaired while the game is running. Non-zero process exit codes are logged. `modloader/modloader-session.json` stores the latest session report. The loader cannot inspect resource reads by the engine, perform live reload, or detect a hung process. After forcibly ending the game, run `--recover`.

## Process and archive safety

Before launching with mods, packing, or recovery, the loader checks whether `dirttown.exe` is running from this game folder's full path. It refuses to replace or restore `data.pack` if the game is running or its state cannot be confirmed. On Windows, the monitor tracks the launched process and observed child processes; normal recovery waits for them to close. If monitoring is interrupted or fails, the backup is kept for manual recovery.

The archive builder streams source files in 1 MiB chunks. The builder and reader reject Windows-invalid paths, including device names, forbidden characters, control characters, and names ending in dots or spaces.

Run source tests with `py -3 -m unittest discover -s tests -v`. Process-tree tests run on Windows. The monitor cannot confirm whether the engine loaded a resource; a process that is outside the observed process tree may require manual handling.

## Steam

The loader runs `steam://run/<AppID>` and reads the AppID from the local `appmanifest_*.acf` matching the installation directory. Use `--steam-appid` to override the detected AppID. After Steam accepts the request, the monitor waits up to 60 seconds by default for a process whose full path matches the game executable, then tracks it and its child processes. `--startup-timeout` changes the wait. If the game process does not appear, the loader records `startup_timeout` and restores the original archive.

## Current file layout (0.7.1)

The active ModLoader executable stays in the game folder. Logs, session report, lock, staged archive, recovery state, and `--build-only` output are stored in `modloader/`. Logs are grouped by local date; the newest ends in `.latest.log`, and startup rotation keeps at most five logs. The user's `data/` folder is independent: `--unpack` and `--pack` use it only when explicitly requested. `--pack` writes `./data.pack`; the optional `data.old` backup stays beside the game archive.

## Version history

### 0.7.1 — current release

Safety fix: after a Steam launch request begins, an error or interruption no longer triggers automatic restoration of `data.pack`, because the game may already be starting. The backup and recovery state remain available through `--recover`. The default Steam startup wait increased to 60 seconds. The monitor briefly waits for child processes to become visible after a launcher exits. The packer creates missing output directories.

### 0.7.0 — previous release

`--pack` refuses to run while the game is active or its state cannot be confirmed, and checks again before replacing the archive. Packing `data/` no longer creates a temporary ZIP and still excludes `data/data.pack`. Validation and unpacking stream payloads in chunks. If the monitor cannot confirm that a tracked process has exited, it keeps the modded archive for manual recovery instead of restoring automatically. The mod manifest requires a Boolean `enabled`; TOML override checks are case-insensitive for the extension.

### 0.6.0 — previous release

Automatic exit after the game closes, a `modloader/` directory for working files, and dated log rotation that keeps up to five files.

### 0.5.0 — previous release

Launches the game through Steam, detects the AppID, and waits for the game process.

### 0.4.0 — previous release

Process-tree monitoring and protection against replacing `data.pack` while the game is running.

### 0.3.0 — previous release

Basic monitoring of the game process, mod changes, and session report.
