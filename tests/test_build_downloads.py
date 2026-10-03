#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Offline regressions for explicit downloads and external build artifacts."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
import asset_fetch
import build_support
from project_paths import ProjectPaths


def sha(data):
    return hashlib.sha256(data).hexdigest()


class PrivateFiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='live-doom-build-test-')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def assert_no_staging(self):
        self.assertFalse(list(self.work.rglob('.download-*')))
        self.assertFalse(list(self.work.rglob('.extract-*')))


class DownloadTests(PrivateFiles):
    def test_verified_publication_cache_and_mismatch_preservation(self):
        data = b'original free game fixture'
        destination = self.work / 'cache/file.zip'

        def worker(arguments, **options):
            Path(arguments[5]).write_bytes(data)
            self.assertEqual(options['timeout'], 90)
            return subprocess.CompletedProcess(arguments, 0, stderr=b'')

        with patch.object(asset_fetch.subprocess, 'run', side_effect=worker) as process:
            self.assertEqual(asset_fetch.fetch_verified('https://example.org/fixture', destination, sha(data), 256), destination)
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            asset_fetch.fetch_verified('https://example.org/fixture', destination, sha(data), 256)
            self.assertEqual(process.call_count, 1, 'Verified cache must not start a fetch worker')
            with self.assertRaisesRegex(ValueError, 'different SHA256'):
                asset_fetch.fetch_verified('https://example.org/fixture', destination, sha(b'new'), 256)
            self.assertEqual(destination.read_bytes(), data)
        self.assert_no_staging()

    def test_deadline_and_worker_hash_failure_never_publish(self):
        destination = self.work / 'download.zip'
        with patch.object(asset_fetch.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 1)):
            with self.assertRaisesRegex(TimeoutError, 'deadline'):
                asset_fetch.fetch_verified('https://example.org/fixture', destination, sha(b'good'), 256)
        self.assertFalse(destination.exists())
        self.assert_no_staging()

        def worker(arguments, **options):
            Path(arguments[5]).write_bytes(b'bad')
            return subprocess.CompletedProcess(arguments, 0, stderr=b'')

        with patch.object(asset_fetch.subprocess, 'run', side_effect=worker):
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                asset_fetch.fetch_verified('https://example.org/fixture', destination, sha(b'good'), 256)
        self.assertFalse(destination.exists())
        self.assert_no_staging()

    def test_symlinks_and_invalid_limits_rejected_without_worker(self):
        outside = self.work / 'outside'
        outside.write_bytes(b'preserved')
        destination = self.work / 'download.zip'
        destination.symlink_to(outside)
        with patch.object(asset_fetch.subprocess, 'run') as worker:
            for arguments in (
                    ('https://example.org/fixture', destination, sha(b'good'), 256),
                    ('http://example.org/fixture', self.work / 'other', sha(b'good'), 256),
                    ('https://example.org/fixture', self.work / 'other', sha(b'good'), True)):
                with self.assertRaises((ValueError, OSError)):
                    asset_fetch.fetch_verified(*arguments)
            for deadline in (float('inf'), float('nan'), True):
                with self.assertRaises(ValueError):
                    asset_fetch.fetch_verified('https://example.org/fixture', self.work / 'other', sha(b'good'), 256, deadline=deadline)
            worker.assert_not_called()
        self.assertEqual(outside.read_bytes(), b'preserved')

    def test_stream_size_hash_and_https_final_url(self):
        class Response(io.BytesIO):
            def __init__(self, data, url='https://example.org/fixture', declared=None):
                super().__init__(data)
                self.url, self.headers = url, {} if declared is None else {'Content-Length': declared}

            def geturl(self):
                return self.url

        temporary = self.work / 'stage'
        temporary.write_bytes(b'')
        for response, limit, error in (
                (Response(b'good'), 4, None),
                (Response(b'large'), 4, 'byte limit'),
                (Response(b'good', declared='100'), 4, 'Content-Length'),
                (Response(b'good', url='http://example.org/fixture'), 4, 'HTTPS'),
                (Response(b'bad'), 4, 'SHA256')):
            with patch.object(asset_fetch, 'build_opener') as opener:
                opener.return_value.open.return_value = response
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        asset_fetch._fetch_stream('https://example.org/fixture', str(temporary), sha(b'good'), limit, 1, 2)
                else:
                    asset_fetch._fetch_stream('https://example.org/fixture', str(temporary), sha(b'good'), limit, 1, 2)
                    self.assertEqual(temporary.read_bytes(), b'good')

    def test_same_host_is_opt_in_and_blocks_redirect_and_final_origin(self):
        from urllib.request import Request
        request = Request('https://example.org/fixture')
        strict = asset_fetch.HTTPSRedirects(request.full_url)
        with self.assertRaisesRegex(ValueError, 'same host'):
            strict.redirect_request(request, None, 302, 'Found', {}, 'https://other.org/fixture')
        allowed = strict.redirect_request(request, None, 302, 'Found', {}, 'https://example.org/new')
        self.assertEqual(allowed.full_url, 'https://example.org/new')
        cross = asset_fetch.HTTPSRedirects().redirect_request(request, None, 302, 'Found', {}, 'https://other.org/new')
        self.assertEqual(cross.full_url, 'https://other.org/new', 'Freedoom CDN redirects remain supported')
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            strict.redirect_request(request, None, 302, 'Found', {}, 'http://example.org/new')

        data = b'good'
        destination = self.work / 'download.zip'

        def worker(arguments, **options):
            self.assertEqual(arguments[-1], '1')
            Path(arguments[5]).write_bytes(data)
            return subprocess.CompletedProcess(arguments, 0, stderr=b'')

        with patch.object(asset_fetch.subprocess, 'run', side_effect=worker):
            asset_fetch.fetch_verified(request.full_url, destination, sha(data), 256, same_host=True)
        class Response(io.BytesIO):
            headers = {}
            def geturl(self):
                return 'https://other.org/fixture'

        temporary = self.work / 'worker-stage'
        temporary.write_bytes(b'')
        with patch.object(asset_fetch, 'build_opener') as opener:
            opener.return_value.open.return_value = Response(data)
            with self.assertRaisesRegex(ValueError, 'same host'):
                asset_fetch._fetch_stream(request.full_url, str(temporary), sha(data), 256, 1, 2, True)


class ZipTests(PrivateFiles):
    def archive(self, members):
        path = self.work / 'archive.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in members:
                archive.writestr(name, data)
        return path, sha(path.read_bytes())

    def install(self, archive, digest, limit=4096):
        return asset_fetch.install_verified_zip(archive, self.work / 'free-fixture', digest,
                                               65536, limit, {'game.wad': sha(b'original')})

    def test_safe_install_reuse_and_existing_tamper_is_preserved(self):
        archive, digest = self.archive([('free-fixture/game.wad', b'original'), ('free-fixture/LICENSE', b'free')])
        destination = self.install(archive, digest)
        self.assertEqual((destination / 'game.wad').read_bytes(), b'original')
        self.assertEqual(stat.S_IMODE((destination / 'game.wad').stat().st_mode), 0o600)
        self.assertEqual(self.install(archive, digest), destination)
        (destination / 'game.wad').write_bytes(b'preserved tamper')
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.install(archive, digest)
        self.assertEqual((destination / 'game.wad').read_bytes(), b'preserved tamper')
        self.assert_no_staging()

    def test_traversal_symlink_duplicate_and_expansion_limit(self):
        link = zipfile.ZipInfo('free-fixture/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for members, limit, error in (
                ([('free-fixture/../outside', b'bad')], 4096, 'Unsafe'),
                ([(link, b'../outside')], 4096, 'Unsafe'),
                ([('free-fixture/game.wad', b'original')] * 2, 4096, 'Unsafe'),
                ([('free-fixture/game.wad', b'original')], 4, 'uncompressed')):
            with self.subTest(error=error):
                # The intentionally duplicated entry may emit a zipfile warning.
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive, digest = self.archive(members)
                with self.assertRaisesRegex(ValueError, error):
                    self.install(archive, digest, limit)
                self.assertFalse((self.work / 'outside').exists())
                self.assertFalse((self.work / 'free-fixture').exists())
                self.assert_no_staging()

    def test_hash_checked_before_archive_parse(self):
        archive = self.work / 'not-a-zip'
        archive.write_bytes(b'bad archive')
        with patch.object(asset_fetch.zipfile, 'ZipFile') as parse:
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                self.install(archive, sha(b'different'))
            parse.assert_not_called()


class BuildScriptTests(PrivateFiles):
    def env(self):
        home = self.work / 'home with spaces'
        home.mkdir(exist_ok=True)
        return os.environ | {'HOME': str(home), 'XDG_DATA_HOME': str(self.work / 'data with spaces'),
                             'XDG_CACHE_HOME': str(self.work / 'cache with spaces'),
                             'PYTHONDONTWRITEBYTECODE': '1'}

    def test_no_flag_bootstrap_and_asset_cli_have_no_fetch_or_output(self):
        env = self.env()
        sentinel = self.work / 'unexpected-git'
        commands = self.work / 'commands'
        commands.mkdir()
        fakegit = commands / 'git'
        fakegit.write_text('#!/bin/sh\ntouch "$GIT_SENTINEL"\nexit 99\n')
        fakegit.chmod(0o700)
        env.update(PATH=str(commands) + os.pathsep + env['PATH'], GIT_SENTINEL=str(sentinel))
        for command in ([str(ROOT / 'scripts/bootstrap.sh')],
                        [sys.executable, '-B', str(ROOT / 'scripts/fetch-assets.py')]):
            result = subprocess.run(command, env=env, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'--fetch-', result.stderr)
            self.assertFalse(sentinel.exists())
            self.assertFalse(Path(env['XDG_DATA_HOME']).exists())
            self.assertFalse(Path(env['XDG_CACHE_HOME']).exists())

    def test_bootstrap_game_data_hint_only_when_default_iwad_is_absent(self):
        paths = ProjectPaths.from_env(home=self.work / 'home', env=self.env())
        for name in build_support.SOURCE_NAMES:
            (paths.vendor_root / name).mkdir(parents=True)
        for installed in (False, True):
            with self.subTest(installed=installed):
                if installed:
                    paths.default_iwad.parent.mkdir(parents=True)
                    # Presence suppresses the hint; it does not prove this file's hash.
                    paths.default_iwad.write_bytes(b'private unverified fixture')
                with patch.object(build_support, 'paths', return_value=paths), \
                        patch.object(build_support, 'read_bytes', return_value=b'private patch fixture'), \
                        patch.object(build_support, 'prior_source_state', return_value=(None, None)), \
                        patch.object(build_support, 'inspect_sources', return_value=({}, True)), \
                        patch.object(build_support, 'prepare_sources') as fetch_sources, \
                        patch.object(build_support.subprocess, 'run') as fetch_assets, \
                        patch('sys.stdout', new_callable=io.StringIO) as output:
                    build_support.bootstrap()
                fetch_sources.assert_not_called()
                fetch_assets.assert_not_called()
                if installed:
                    self.assertEqual(output.getvalue(), '')
                    self.assertEqual(paths.default_iwad.read_bytes(), b'private unverified fixture')
                else:
                    self.assertIn('--fetch-freedoom', output.getvalue())
                    self.assertFalse(paths.default_iwad.exists())

    def test_missing_cmake_and_malformed_jobs_fail_before_writes(self):
        env = self.env()
        env['CMAKE'] = str(self.work / 'missing-cmake')
        result = subprocess.run([str(ROOT / 'scripts/build.sh')], env=env, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'No tool is downloaded', result.stderr)
        cmake = self.work / 'cmake'
        cmake.write_text('#!/bin/sh\nprintf "cmake version 3.28.1\\n"\n')
        cmake.chmod(0o700)
        env['CMAKE'] = str(cmake)
        for jobs in ('0', '33', '9999999999999999999999999999999', 'abc'):
            result = subprocess.run([str(ROOT / 'scripts/build.sh')], env=env | {'LIVE_DOOM_BUILD_JOBS': jobs},
                                    capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'1 to 32', result.stderr)
        self.assertFalse(Path(env['XDG_DATA_HOME']).exists())
        self.assertFalse(Path(env['XDG_CACHE_HOME']).exists())

    def test_external_layout_header_and_regular_atomic_executable(self):
        env = self.env()
        with patch.dict(os.environ, env, clear=True):
            paths = build_support.paths()
            build_support.prepare_layout(paths)
            build_support.stage_header(paths)
            copied = paths.data_root / 'src/bridge.h'
            self.assertEqual(sha(copied.read_bytes()), sha((ROOT / 'src/bridge.h').read_bytes()))
            original_mtime = copied.stat().st_mtime_ns
            build_support.stage_header(paths)
            self.assertEqual(copied.stat().st_mtime_ns, original_mtime, 'Unchanged headers must not trigger native recompilation')
            source = self.work / 'fixture executable'
            source.write_bytes(b'new compiled fixture')
            build_support.publish(source, paths.engine)
            self.assertFalse(paths.engine.is_symlink())
            self.assertEqual(paths.engine.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(paths.engine.stat().st_mode), 0o700)
            self.assertFalse(ROOT in paths.engine.parents)
            paths.viewer.symlink_to(source)
            with self.assertRaises((ValueError, OSError)):
                build_support.publish(source, paths.viewer)
            with patch.dict(os.environ, {'XDG_DATA_HOME': str(ROOT)}):
                with self.assertRaisesRegex(ValueError, 'outside'):
                    build_support.paths()

    def test_git_capture_is_bounded_and_reaped(self):
        real_popen = subprocess.Popen
        with patch.object(build_support.subprocess, 'Popen',
                          side_effect=lambda *args, **kwargs: real_popen([sys.executable, '-B', '-c',
                              'import sys; sys.stdout.buffer.write(b"x" * (4*1024*1024+1))'], **kwargs)):
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                build_support.git(self.work, 'diff', capture=True)


class SourceUpdateTests(PrivateFiles):
    """Real tiny local repositories exercise upgrades without any network."""
    def setUp(self):
        super().setUp()
        self.paths = ProjectPaths.from_env(home=self.work / 'home', env={
            'XDG_DATA_HOME': str(self.work / 'data'), 'XDG_CACHE_HOME': str(self.work / 'cache')})
        self.repositories = self.work / 'local-remotes'
        self.repositories.mkdir()
        old, new = {}, {}
        for name in ('adlmidi', 'SDL_mixer', 'SDL_net', 'autodoom'):
            repo = self.repositories / name
            repo.mkdir()
            self.git(repo, 'init', '--quiet')
            (repo / 'CMakeLists.txt').write_text('# original fixture\n')
            (repo / '.gitignore').write_text('*.local\n')
            (repo / 'payload').write_bytes(b'old original data')
            if name == 'autodoom':
                (repo / 'source').mkdir()
                (repo / 'source/base.cpp').write_text('// original fixture source\n')
                (repo / '.gitmodules').write_text('[submodule "adlmidi"]\n path = adlmidi\n url = ' + str(self.repositories / 'adlmidi') + '\n')
            self.git(repo, 'add', '.')
            if name == 'autodoom':
                self.git(repo, 'update-index', '--add', '--cacheinfo', '160000,' + old['adlmidi']['revision'] + ',adlmidi')
            self.git(repo, 'commit', '--quiet', '-m', 'Original fixture version one')
            old[name] = {'url': str(repo), 'revision': self.git(repo, 'rev-parse', 'HEAD').strip()}
            (repo / 'payload').write_bytes(b'new original data')
            self.git(repo, 'add', '.')
            if name == 'autodoom':
                self.git(repo, 'update-index', '--add', '--cacheinfo', '160000,' + new['adlmidi']['revision'] + ',adlmidi')
            self.git(repo, 'commit', '--quiet', '-m', 'Original fixture version two')
            new[name] = {'url': str(repo), 'revision': self.git(repo, 'rev-parse', 'HEAD').strip()}
        self.old, self.new = {'sources': old}, {'sources': new}
        engine = self.repositories / 'autodoom'
        for suffix in ('cpp', 'h'):
            (engine / 'source' / ('desktop_bridge.' + suffix)).write_text('// old original adapter fixture\n')
        self.old_patch = build_support.working_patch(engine)
        for suffix in ('cpp', 'h'):
            (engine / 'source' / ('desktop_bridge.' + suffix)).write_text('// new original adapter fixture\n')
        self.new_patch = build_support.working_patch(engine)
        mixer = self.repositories / 'SDL_mixer'
        (mixer / 'CMakeLists.txt').write_text('# original fixture\n# old owned device adapter\n')
        self.old_mixer_patch = build_support.working_mixer_patch(mixer)
        (mixer / 'CMakeLists.txt').write_text('# original fixture\n# new owned device adapter\n')
        self.new_mixer_patch = build_support.working_mixer_patch(mixer)

    def git(self, path, *arguments):
        result = subprocess.run(['/usr/bin/git', '-c', 'user.name=Private Fixture',
                                 '-c', 'user.email=fixture@localhost', '-C', str(path), *arguments],
                                capture_output=True, text=True, check=True, timeout=5,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(self.work),
                                     'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'})
        return result.stdout

    def prepare(self, metadata, desktop_patch, mixer_patch=b''):
        with patch.dict(os.environ, {'HOME': str(self.work), 'GIT_CONFIG_NOSYSTEM': '1'}), \
                patch('sys.stdout', new_callable=io.StringIO):
            build_support.prepare_sources(self.paths, metadata, desktop_patch, mixer_patch)

    def legacy_receipt(self, metadata, desktop_patch):
        """Reproduce the exact schema-1 receipt shipped before the mixer patch."""
        state = {'schema': 1, 'sources': metadata['sources'], 'patch_sha256': sha(desktop_patch)}
        (self.paths.vendor_root / build_support.SOURCE_RECEIPT).write_text(json.dumps(state))
        (self.paths.vendor_root / build_support.OWNED_MIXER_PATCH).unlink()
        return (self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes()

    def revisions(self):
        return {name: self.git(self.paths.vendor_root / ('autodoom/adlmidi' if name == 'adlmidi' else name),
                              'rev-parse', 'HEAD').strip() for name in self.old['sources']}

    def test_owned_old_revision_and_patch_upgrade_retains_previous_trees(self):
        self.prepare(self.old, self.old_patch)
        self.prepare(self.new, self.new_patch)
        self.assertEqual(self.revisions(), {name: pin['revision'] for name, pin in self.new['sources'].items()})
        self.assertEqual(build_support.working_patch(self.paths.vendor_root / 'autodoom'), self.new_patch)
        backups = list((self.paths.data_root / 'source-backups').glob('sources-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.git(backups[0] / 'autodoom', 'rev-parse', 'HEAD').strip(), self.old['sources']['autodoom']['revision'])
        self.assertEqual((backups[0] / build_support.OWNED_PATCH).read_bytes(), self.old_patch)
        with patch.object(build_support, 'source', side_effect=AssertionError('No unchanged fetch')):
            self.prepare(self.new, self.new_patch)

    def test_schema1_receipt_upgrades_pristine_mixer_and_preserves_old_receipt(self):
        self.prepare(self.old, self.old_patch)
        old_receipt = self.legacy_receipt(self.old, self.old_patch)
        self.prepare(self.old, self.old_patch, self.new_mixer_patch)
        self.assertEqual(self.revisions(), {name: pin['revision'] for name, pin in self.old['sources'].items()})
        self.assertEqual(build_support.working_mixer_patch(self.paths.vendor_root / 'SDL_mixer'), self.new_mixer_patch)
        state, previous_patch = build_support.prior_source_state(self.paths)
        self.assertEqual(state['schema'], 2)
        self.assertEqual(state['patches'], {'autodoom': sha(self.old_patch), 'SDL_mixer': sha(self.new_mixer_patch)})
        self.assertEqual(previous_patch, self.old_patch)
        backups = list((self.paths.data_root / 'source-backups').glob('sources-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / build_support.SOURCE_RECEIPT).read_bytes(), old_receipt)
        self.assertFalse((backups[0] / build_support.OWNED_MIXER_PATCH).exists())
        self.assertEqual(build_support.working_mixer_patch(backups[0] / 'SDL_mixer'), b'')

    def test_schema1_receipt_can_migrate_without_fetch_when_sources_already_exact(self):
        self.prepare(self.old, self.old_patch)
        self.legacy_receipt(self.old, self.old_patch)
        with patch.object(build_support, 'source', side_effect=AssertionError('No source replacement needed')):
            self.prepare(self.old, self.old_patch)
        state, _ = build_support.prior_source_state(self.paths)
        self.assertEqual(state['schema'], 2)
        self.assertEqual(state['patches']['SDL_mixer'], sha(b''))

    def test_owned_mixer_patch_update_retains_exact_previous_mixer_and_patch(self):
        self.prepare(self.old, self.old_patch, self.old_mixer_patch)
        self.prepare(self.old, self.old_patch, self.new_mixer_patch)
        self.assertEqual(build_support.working_mixer_patch(self.paths.vendor_root / 'SDL_mixer'), self.new_mixer_patch)
        backups = list((self.paths.data_root / 'source-backups').glob('sources-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(build_support.working_mixer_patch(backups[0] / 'SDL_mixer'), self.old_mixer_patch)
        self.assertEqual((backups[0] / build_support.OWNED_MIXER_PATCH).read_bytes(), self.old_mixer_patch)
        with patch.object(build_support, 'source', side_effect=AssertionError('No unchanged fetch')):
            self.prepare(self.old, self.old_patch, self.new_mixer_patch)

    def test_mixer_patch_edits_and_invalid_receipt_abort_before_fetch(self):
        self.prepare(self.old, self.old_patch, self.old_mixer_patch)
        mixer = self.paths.vendor_root / 'SDL_mixer'
        recorded = self.paths.vendor_root / build_support.OWNED_MIXER_PATCH
        receipt = self.paths.vendor_root / build_support.SOURCE_RECEIPT
        source = mixer / 'CMakeLists.txt'
        original_source, original_receipt = source.read_bytes(), receipt.read_bytes()
        cases = [('tracked edit', source, b'personal adapter edit', 'Unknown source edits'),
                 ('recorded patch edit', recorded, b'unknown stored patch', 'receipt is invalid')]
        for name, path, replacement, message in cases:
            original = path.read_bytes()
            path.write_bytes(replacement)
            with self.subTest(name=name), patch.object(build_support, 'source') as fetch:
                with self.assertRaisesRegex(ValueError, message):
                    self.prepare(self.old, self.old_patch, self.new_mixer_patch)
                fetch.assert_not_called()
                self.assertEqual(path.read_bytes(), replacement)
                self.assertEqual(receipt.read_bytes(), original_receipt)
            path.write_bytes(original)
        self.git(mixer, 'add', 'CMakeLists.txt')
        with patch.object(build_support, 'source') as fetch:
            with self.assertRaisesRegex(ValueError, 'Unknown source edits'):
                self.prepare(self.old, self.old_patch, self.new_mixer_patch)
            fetch.assert_not_called()
        self.assertEqual(source.read_bytes(), original_source)

    def test_schema1_receipt_does_not_claim_an_unknown_mixer_adapter(self):
        self.prepare(self.old, self.old_patch, self.old_mixer_patch)
        original = self.legacy_receipt(self.old, self.old_patch)
        with patch.object(build_support, 'source') as fetch:
            with self.assertRaisesRegex(ValueError, 'Unknown source edits'):
                self.prepare(self.old, self.old_patch, self.new_mixer_patch)
            fetch.assert_not_called()
        self.assertEqual((self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes(), original)
        self.assertEqual(build_support.working_mixer_patch(self.paths.vendor_root / 'SDL_mixer'), self.old_mixer_patch)

    def test_failed_new_mixer_patch_never_replaces_old_sources(self):
        self.prepare(self.old, self.old_patch, self.old_mixer_patch)
        original = self.revisions()
        receipt = (self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes()
        with self.assertRaises(RuntimeError):
            self.prepare(self.new, self.new_patch, b'not a usable mixer patch\n')
        self.assertEqual(self.revisions(), original)
        self.assertEqual(build_support.working_mixer_patch(self.paths.vendor_root / 'SDL_mixer'), self.old_mixer_patch)
        self.assertEqual((self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes(), receipt)
        self.assertFalse(list(self.paths.data_root.glob('.source-update-*')))

    def test_mixer_update_requires_explicit_source_fetch_opt_in(self):
        self.prepare(self.old, self.old_patch, self.old_mixer_patch)
        root = self.work / 'plugin-fixture'
        (root / 'patches').mkdir(parents=True)
        (root / 'patches/autodoom-desktop.patch').write_bytes(self.old_patch)
        (root / 'patches/sdl-mixer-device.patch').write_bytes(self.new_mixer_patch)
        receipt = (self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes()
        with patch.object(build_support, 'ROOT', root), patch.object(build_support, 'paths', return_value=self.paths), \
                patch.object(build_support, 'pins', return_value=self.old), patch.object(build_support, 'source') as fetch:
            with self.assertRaisesRegex(ValueError, '--fetch-sources'):
                build_support.bootstrap()
            fetch.assert_not_called()
        self.assertEqual(build_support.working_mixer_patch(self.paths.vendor_root / 'SDL_mixer'), self.old_mixer_patch)
        self.assertEqual((self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes(), receipt)

    def test_mixer_record_publication_failure_rolls_back_sources_and_both_records(self):
        self.prepare(self.old, self.old_patch, self.old_mixer_patch)
        receipt = (self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes()
        original = self.revisions()
        replace = os.replace
        def reject_record(source, destination, *args, **kwargs):
            source = Path(source)
            if source.name == build_support.OWNED_MIXER_PATCH and source.parent.name.startswith('.source-update-'):
                raise OSError('private mixer record rename failure')
            return replace(source, destination, *args, **kwargs)
        with patch.object(build_support.os, 'replace', side_effect=reject_record):
            with self.assertRaisesRegex(OSError, 'private mixer record rename failure'):
                self.prepare(self.new, self.new_patch, self.new_mixer_patch)
        self.assertEqual(self.revisions(), original)
        self.assertEqual(build_support.working_mixer_patch(self.paths.vendor_root / 'SDL_mixer'), self.old_mixer_patch)
        self.assertEqual((self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes(), receipt)
        self.assertEqual((self.paths.vendor_root / build_support.OWNED_PATCH).read_bytes(), self.old_patch)
        self.assertEqual((self.paths.vendor_root / build_support.OWNED_MIXER_PATCH).read_bytes(), self.old_mixer_patch)

    def test_unknown_tracked_untracked_ignored_and_bridge_edits_abort_before_fetch(self):
        self.prepare(self.old, self.old_patch)
        original = self.revisions()
        receipt = (self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes()
        cases = [(self.paths.vendor_root / 'SDL_net/payload', b'personal tracked edit'),
                 (self.paths.vendor_root / 'SDL_mixer/personal.txt', b'personal untracked file'),
                 (self.paths.vendor_root / 'SDL_mixer/settings.local', b'personal ignored file'),
                 (self.paths.vendor_root / 'autodoom/source/desktop_bridge.cpp', b'personal bridge edit')]
        for path, data in cases:
            prior = path.read_bytes() if path.exists() else None
            path.write_bytes(data)
            with self.subTest(path=path.name), patch.object(build_support, 'source') as fetch:
                with self.assertRaisesRegex(ValueError, 'Unknown source edits'):
                    self.prepare(self.new, self.new_patch)
                fetch.assert_not_called()
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(self.revisions(), original)
                self.assertEqual((self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes(), receipt)
            if prior is None:
                path.unlink()
            else:
                path.write_bytes(prior)

    def test_unjournaled_pristine_older_sources_can_upgrade(self):
        build_support.prepare_layout(self.paths)
        with patch('sys.stdout', new_callable=io.StringIO):
            for name in build_support.SOURCE_NAMES:
                build_support.source(self.paths.vendor_root / name, self.old['sources'][name], True)
            build_support.source(self.paths.vendor_root / 'autodoom/adlmidi', self.old['sources']['adlmidi'], True)
        self.prepare(self.new, self.new_patch)
        self.assertEqual(self.revisions(), {name: pin['revision'] for name, pin in self.new['sources'].items()})

    def test_unknown_unjournaled_prior_patch_is_preserved_and_rejected(self):
        self.prepare(self.old, self.old_patch)
        (self.paths.vendor_root / build_support.SOURCE_RECEIPT).unlink()
        (self.paths.vendor_root / build_support.OWNED_PATCH).unlink()
        (self.paths.vendor_root / build_support.OWNED_MIXER_PATCH).unlink()
        with patch.object(build_support, 'source') as fetch:
            with self.assertRaisesRegex(ValueError, 'safe backup'):
                self.prepare(self.new, self.new_patch)
            fetch.assert_not_called()
        self.assertEqual(build_support.working_patch(self.paths.vendor_root / 'autodoom'), self.old_patch)

    def test_failed_new_patch_never_replaces_old_sources(self):
        self.prepare(self.old, self.old_patch)
        original = self.revisions()
        receipt = (self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes()
        with self.assertRaises(RuntimeError):
            self.prepare(self.new, b'not a usable desktop patch\n')
        self.assertEqual(self.revisions(), original)
        self.assertEqual((self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes(), receipt)
        self.assertFalse(list(self.paths.data_root.glob('.source-update-*')))

    def test_publication_failure_rolls_back_all_active_sources(self):
        self.prepare(self.old, self.old_patch)
        original = self.revisions()
        receipt = (self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes()
        replace = os.replace
        def reject_one(source, destination, *args, **kwargs):
            source = Path(source)
            if source.name == 'SDL_mixer' and source.parent.name.startswith('.source-update-'):
                raise OSError('private rename failure')
            return replace(source, destination, *args, **kwargs)
        with patch.object(build_support.os, 'replace', side_effect=reject_one):
            with self.assertRaisesRegex(OSError, 'private rename failure'):
                self.prepare(self.new, self.new_patch)
        self.assertEqual(self.revisions(), original)
        self.assertEqual((self.paths.vendor_root / build_support.SOURCE_RECEIPT).read_bytes(), receipt)

    def test_edit_during_staging_is_revalidated_and_preserved(self):
        self.prepare(self.old, self.old_patch)
        original = self.revisions()
        fetch = build_support.source
        personal = self.paths.vendor_root / 'SDL_net/payload'
        def edit_during_stage(directory, pin, explicit=False):
            result = fetch(directory, pin, explicit)
            if directory.name == 'adlmidi':
                personal.write_bytes(b'edited while new sources staged')
            return result
        with patch.object(build_support, 'source', side_effect=edit_during_stage):
            with self.assertRaisesRegex(ValueError, 'Unknown source edits'):
                self.prepare(self.new, self.new_patch)
        self.assertEqual(personal.read_bytes(), b'edited while new sources staged')
        self.assertEqual(self.revisions(), original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
