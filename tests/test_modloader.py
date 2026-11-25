import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

ML = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ML))

import modloader
import monitor
import pack_digp
import unpack_digp


class ArchiveTests(unittest.TestCase):
    def test_packer_creates_missing_output_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'asset.bin').write_bytes(b'asset')
            output = root / 'new' / 'nested' / 'data.pack'

            pack_digp.build(source, output)

            self.assertTrue(output.is_file())

    def test_streamed_directory_round_trip_across_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            # Both compressed and raw payloads cross the 1 MiB streaming boundary.
            payload = os.urandom(1_500_123)
            (source / 'vehicle.toml').write_bytes(payload)
            (source / 'texture.png').write_bytes(payload)
            archive = root / 'data.pack'

            pack_digp.build(source, archive)
            with archive.open('rb') as stream:
                records = list(unpack_digp.entries(stream))
                for name, flag, pos, plain, stored in records:
                    data = b''.join(unpack_digp.payload_chunks(
                        stream, flag, pos, plain, stored, chunk_size=65_537,
                    ))
                    self.assertEqual(len(data), plain)
                    self.assertEqual(data, (source / name).read_bytes())

    def test_archive_paths_reject_windows_devices_and_invalid_chars(self):
        for name in ('textures/bad?.png', 'CON.txt', 'models/trailing./x', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                pack_digp.validate(name)

    def test_mod_overlay_streams_large_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'asset.bin').write_bytes(b'original')
            pack_digp.build(source, root / 'data.pack')
            replacement = root / 'replacement.bin'
            replacement_data = os.urandom(1_300_019)
            replacement.write_bytes(replacement_data)

            modloader.build(root, root / 'modded.pack', {'asset.bin': ('TestMod', replacement)})

            with (root / 'modded.pack').open('rb') as stream:
                record = list(unpack_digp.entries(stream))[0]
                _, flag, pos, plain, stored = record
                stream.seek(pos)
                payload = unpack_digp.decode(stream.read(stored), pos)
            self.assertEqual(flag, 1)
            self.assertEqual(zlib.decompress(payload, -15), replacement_data)
            self.assertEqual(plain, len(replacement_data))


class RecoveryTests(unittest.TestCase):
    def make_recovery_state(self, root, current):
        original = b'original archive'
        (root / 'data.pack.modloader-original').write_bytes(original)
        (root / 'data.pack').write_bytes(current)
        (root / 'data.pack.modloader-state.json').write_text(json.dumps({
            'original': hashlib.sha256(original).hexdigest(),
            'staged': hashlib.sha256(b'modded archive').hexdigest(),
        }), encoding='utf-8')
        (root / 'data.pack.modloader-staged').write_bytes(b'partial stage')

    def test_recover_restores_verified_backup_and_cleans_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_recovery_state(root, b'modded archive')
            modloader.recover(root)
            self.assertEqual((root / 'data.pack').read_bytes(), b'original archive')
            self.assertFalse((root / 'data.pack.modloader-original').exists())
            self.assertFalse((root / 'data.pack.modloader-state.json').exists())
            self.assertFalse((root / 'data.pack.modloader-staged').exists())

    def test_recover_preserves_external_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_recovery_state(root, b'changed outside loader')
            with self.assertRaisesRegex(RuntimeError, 'changed externally'):
                modloader.recover(root)
            self.assertEqual((root / 'data.pack').read_bytes(), b'changed outside loader')
            self.assertEqual((root / 'data.pack.modloader-original').read_bytes(), b'original archive')

    def test_launch_refuses_an_already_running_game(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dirttown.exe').touch()
            with patch.object(modloader, 'matching_process_state', return_value=([1234], [])):
                with self.assertRaisesRegex(RuntimeError, 'already running'):
                    modloader.ensure_game_stopped(root)

    def test_launch_refuses_an_unobservable_same_name_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dirttown.exe').touch()
            with patch.object(modloader, 'matching_process_state', return_value=([], [4321])):
                with self.assertRaisesRegex(RuntimeError, 'Cannot confirm'):
                    modloader.ensure_game_stopped(root)

    def test_steam_app_id_is_read_from_local_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'steamapps' / 'common' / 'Dirt Town Demo'
            manifest_dir = root.parent.parent
            manifest_dir.mkdir(parents=True)
            (manifest_dir / 'appmanifest_5471710.acf').write_text(
                '"AppState" {\n\t"appid" "5471710"\n\t"name" "Dirt Town Demo"\n\t"installdir" "Dirt Town Demo"\n}',
                encoding='utf-8',
            )
            self.assertEqual(modloader.find_steam_app_id(root), '5471710')

    def test_failed_steam_request_preserves_recovery_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dirttown.exe').touch()
            source = root / 'source'
            (source / 'machines').mkdir(parents=True)
            (source / 'machines' / 'truck.toml').write_text('value = 1\n', encoding='utf-8')
            pack_digp.build(source, root / 'data.pack')
            override = root / 'mods' / 'BetterTruck' / 'machines' / 'truck.toml'
            override.parent.mkdir(parents=True)
            override.write_text('value = 2\n', encoding='utf-8')

            with patch.object(modloader, 'find_steam_app_id', return_value='5471710'), patch.object(
                modloader, 'ensure_game_stopped', return_value=None,
            ), patch.object(modloader, 'snapshot', return_value={}), patch.object(
                modloader, 'start_steam', side_effect=OSError('Steam launch request failed'),
            ):
                with self.assertRaisesRegex(OSError, 'Steam launch request failed'):
                    modloader.launch(root)

            self.assertTrue((root / 'data.pack').exists())
            self.assertTrue((root / 'modloader' / modloader.BACKUP).exists())
            self.assertTrue((root / 'modloader' / modloader.STATE).exists())


class ModValidationTests(unittest.TestCase):
    def test_uppercase_toml_override_is_syntax_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            override = root / 'mods' / 'Broken' / 'machines' / 'truck.TOML'
            override.parent.mkdir(parents=True)
            override.write_text('broken = [', encoding='utf-8')

            with self.assertRaises(ValueError):
                modloader.mods(root, {'machines/truck.TOML'})

    def test_manifest_enabled_must_be_a_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'mods' / 'BadManifest' / 'mod.toml'
            manifest.parent.mkdir(parents=True)
            manifest.write_text('[mod]\nenabled = "false"\n', encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'expected true or false'):
                modloader.mods(root, set())


class RuntimeArtifactTests(unittest.TestCase):
    def test_log_rotation_keeps_five_files_and_one_latest_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / 'modloader' / 'logs'
            base_time = time.time_ns() - 10_000_000_000
            for index in range(6):
                day = logs / f'2025-01-{index + 1:02d}'
                day.mkdir(parents=True)
                suffix = '.latest.log' if index == 5 else '.log'
                path = day / f'12-00-00-{index:06d}{suffix}'
                path.write_text(f'old log {index}', encoding='utf-8')
                os.utime(path, ns=(base_time + index, base_time + index))

            handler = modloader.setup_log_file(root)
            handler.close()
            files = list(logs.rglob('*.log'))

            self.assertLessEqual(len(files), 5)
            self.assertEqual(sum(path.name.endswith('.latest.log') for path in files), 1)

    def test_legacy_runtime_files_are_migrated_under_modloader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'modloader.log').write_text('old log', encoding='utf-8')
            (root / 'modloader-session.json').write_text('{"status":"exited"}', encoding='utf-8')
            (root / 'modloader.lock').write_bytes(b'0')

            with modloader.lock(root):
                modloader.migrate_legacy_session(root)
                handler = modloader.setup_log_file(root)
                handler.close()

            self.assertFalse((root / 'modloader.log').exists())
            self.assertFalse((root / 'modloader-session.json').exists())
            self.assertFalse((root / 'modloader.lock').exists())
            self.assertEqual((root / 'modloader' / 'modloader-session.json').read_text(encoding='utf-8'), '{"status":"exited"}')
            self.assertTrue(any(path.read_text(encoding='utf-8') == 'old log' for path in (root / 'modloader' / 'logs').rglob('*.log')))
            self.assertTrue(any((root / 'modloader' / 'legacy').glob('modloader.lock-*')))


class PackCommandTests(unittest.TestCase):
    def prepare(self, root):
        data = root / 'data'
        data.mkdir()
        (data / 'asset.txt').write_text('from unpacked data', encoding='utf-8')
        # The legacy nested output must not be packed as a game resource.
        (data / 'data.pack').write_bytes(b'legacy nested pack')

    def resource_names(self, archive):
        with archive.open('rb') as stream:
            return [record[0] for record in unpack_digp.entries(stream)]

    def test_pack_replaces_root_archive_and_saves_data_old(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            old = root / 'data.pack'
            pack_digp.build(root / 'data', old)
            previous = old.read_bytes()

            modloader.pack_game_data(root)

            self.assertEqual((root / 'data.old').read_bytes(), previous)
            self.assertEqual(self.resource_names(root / 'data.pack'), ['asset.txt'])
            self.assertFalse((root / 'data' / 'data.pack').samefile(root / 'data.pack'))

    def test_pack_no_backup_replaces_root_archive_without_creating_data_old(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            old = root / 'data.pack'
            old.write_bytes(b'previous pack')

            modloader.pack_game_data(root, no_backup=True)

            self.assertFalse((root / 'data.old').exists())
            self.assertEqual(self.resource_names(root / 'data.pack'), ['asset.txt'])

    def test_pack_refuses_to_overwrite_an_existing_data_old(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            (root / 'data.pack').write_bytes(b'previous pack')
            (root / 'data.old').write_bytes(b'keep my old backup')

            with self.assertRaisesRegex(FileExistsError, 'data.old already exists'):
                modloader.pack_game_data(root)

            self.assertEqual((root / 'data.pack').read_bytes(), b'previous pack')
            self.assertEqual((root / 'data.old').read_bytes(), b'keep my old backup')

    def test_pack_refuses_to_start_when_game_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'data.pack'
            original.write_bytes(b'leave unchanged')
            with patch.object(modloader, 'ensure_game_stopped', side_effect=RuntimeError('game is running')):
                with self.assertRaisesRegex(RuntimeError, 'game is running'):
                    modloader.pack_game_data(root)
            self.assertEqual(original.read_bytes(), b'leave unchanged')

    def test_pack_rechecks_game_before_final_archive_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            pack_digp.build(root / 'data', root / 'data.pack')
            previous = (root / 'data.pack').read_bytes()
            checks = [None, None, None, RuntimeError('game started during packing')]

            with patch.object(modloader, 'ensure_game_stopped', side_effect=checks) as guard:
                with self.assertRaisesRegex(RuntimeError, 'game started during packing'):
                    modloader.pack_game_data(root)

            self.assertEqual(guard.call_count, 4)
            self.assertEqual((root / 'data.pack').read_bytes(), previous)
            self.assertFalse((root / 'data.old').exists())


@unittest.skipUnless(os.name == 'nt', 'Windows process tree tracking')
class ProcessTreeTests(unittest.TestCase):
    def test_supervisor_waits_for_child_after_launcher_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mods').mkdir()
            wrapper = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(1.5)']); time.sleep(0.2)"
            process = subprocess.Popen([sys.executable, '-c', wrapper], cwd=root)
            started = time.monotonic()
            result = monitor.supervise(process, root, {}, interval=0.2, heartbeat=60)
            elapsed = time.monotonic() - started
            self.assertEqual(result, 0)
            self.assertGreaterEqual(elapsed, 1.2)
            report = json.loads((root / 'modloader' / 'modloader-session.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'exited')

    def test_startup_timeout_tracks_game_launched_outside_launcher_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mods').mkdir()
            launcher = subprocess.Popen([sys.executable, '-c', 'pass'], cwd=root)
            game = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.7)'], cwd=root)
            with patch.object(monitor, 'matching_process_state', side_effect=lambda _: ([game.pid], []) if game.poll() is None else ([], [])):
                started = time.monotonic()
                result = monitor.supervise(
                    launcher, root, {}, interval=0.1, heartbeat=60,
                    game_executable=Path('steam-launched-game.exe'), startup_timeout=2,
                )
            self.assertEqual(result, 0)
            self.assertGreaterEqual(time.monotonic() - started, 0.5)
            report = json.loads((root / 'modloader' / 'modloader-session.json').read_text(encoding='utf-8'))
            self.assertEqual(report['game_pid'], game.pid)
            self.assertEqual(report['status'], 'exited')

    def test_startup_timeout_reports_when_steam_never_starts_game(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mods').mkdir()
            with patch.object(monitor, 'matching_process_state', return_value=([], [])):
                result = monitor.supervise(
                    None, root, {}, interval=0.1, heartbeat=60,
                    game_executable=Path('missing-game.exe'), startup_timeout=0.2,
                )
            self.assertEqual(result, 1)
            report = json.loads((root / 'modloader' / 'modloader-session.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'startup_timeout')


class ProcessUncertaintyTests(unittest.TestCase):
    def test_process_tree_keeps_openprocess_failure_as_unknown(self):
        class DeniedKernel:
            def OpenProcess(self, access, inherit, pid):
                return None

        tree = monitor.ProcessTree()
        tree.kernel32 = DeniedKernel()
        tree.observed.add(1234)
        with patch.object(monitor, '_windows_processes', return_value={1234: (1, 'dirttown.exe')}):
            self.assertEqual(tree.active(False), [])
        self.assertEqual(tree.unknown, {1234})

    def test_wait_failure_does_not_drop_a_live_process_handle(self):
        class FailedWaitKernel:
            def WaitForSingleObject(self, handle, milliseconds):
                return 0xFFFFFFFF  # WAIT_FAILED

        tree = monitor.ProcessTree()
        tree.kernel32 = FailedWaitKernel()
        handle = object()
        tree.handles[1234] = handle
        tree.observed.add(1234)
        with patch.object(monitor, '_windows_processes', return_value={1234: (1, 'dirttown.exe')}):
            self.assertEqual(tree.active(False), [])
        self.assertEqual(tree.unknown, {1234})
        self.assertIs(tree.handles[1234], handle)

    def test_process_path_lookup_failure_is_reported_as_unknown(self):
        with patch.object(monitor, '_windows_processes', return_value={1234: (1, 'dirttown.exe')}), patch.object(
            monitor, '_image_path', return_value=None,
        ):
            matching, unknown = monitor.matching_process_state(Path('G:/game/dirttown.exe'))

        self.assertEqual(matching, [])
        self.assertEqual(unknown, [1234])

    def test_unobservable_game_process_is_not_reported_as_success(self):
        class UncertainTree:
            def __init__(self, root_pid=None):
                self.exit_codes = {}
                self.unknown = set()
                self.calls = 0

            def add_root(self, pid):
                pass

            def active(self, root_running):
                self.calls += 1
                self.unknown = {1234} if self.calls == 1 else set()
                return []

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(monitor, 'ProcessTree', UncertainTree), patch.object(
                monitor, 'matching_process_state', return_value=([1234], []),
            ):
                result = monitor.supervise(
                    None, root, {}, interval=0.1, heartbeat=60,
                    game_executable=Path('game.exe'), startup_timeout=2,
                )

            report = json.loads((root / 'modloader' / 'modloader-session.json').read_text(encoding='utf-8'))
            self.assertEqual(result, 1)
            self.assertEqual(report['status'], 'nonzero_exit')
            self.assertFalse(report['exit_code_observed'])


if __name__ == '__main__':
    unittest.main()
