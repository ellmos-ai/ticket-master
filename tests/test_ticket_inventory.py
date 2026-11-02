import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from lib.ticket_inventory import capture_inventory


def _backend_type():
    from lib import ticket_inventory
    return ticket_inventory._WindowsHandles if os.name == "nt" else ticket_inventory._PosixHandles


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        command = f'mklink /J {link} {target}'
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", command],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def test_complete_source_coverage_and_exact_byte_sections(tmp_path):
    raw = b'\xef\xbb\xbfID: T-20261025-04\r\n\r\nUmlaut: \xc3\xa4\xc3\xb6\xc3\xbc\r\n'
    locations = [
        'T-20261025-01.txt',
        'INBOX/T-20261025-04.txt',
        'INBOX/_formalisiert/raw.txt',
        'PENDING/legacy.txt',
        '.USER/old.txt',
        '_ATTACHMENTS/note.md',
        'unknown/relevant.md',
    ]
    for location in locations:
        path = tmp_path / location
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    for directory in ('SOLVED', 'DONE', '_archive'):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'evidence.txt').write_text('historical', encoding='utf-8')
    (tmp_path / '_logs').mkdir()
    (tmp_path / '_logs' / 'T-20261025-99.txt').write_text(
        'ID: T-20261025-99\nSTATUS: historical log record\n', encoding='utf-8'
    )
    (tmp_path / 'INBOX' / '_logs').mkdir()
    (tmp_path / 'INBOX' / '_logs' / 'nested.log').write_text('nested log evidence', encoding='utf-8')

    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob('*')
        if path.is_file()
    }
    report = capture_inventory(tmp_path, include_content=True)
    assert report['errors'] == []
    assert {row['path'] for row in report['sources']} == set(locations) | {
        '_logs/T-20261025-99.txt', 'INBOX/_logs/nested.log'
    }
    assert {row['path'] for row in report['excluded']} == {'SOLVED', 'DONE', '_archive'}
    log_rows = [row for row in report['sources'] if row['source_class'] == 'historical-log-evidence']
    assert {row['path'] for row in log_rows} == {'_logs/T-20261025-99.txt', 'INBOX/_logs/nested.log'}
    assert all(row['kind'] == 'ancillary' for row in log_rows)
    assert not any(row['kind'] == 'ticket' for row in log_rows)
    assert not report['archive_go']
    assert report['state'] == 'SEMANTIC_REVIEW_REQUIRED'
    for row in report['sources']:
        source_raw = before[row['path']]
        assert row['sha256'] == hashlib.sha256(source_raw).hexdigest()
        if row['path'] in locations:
            assert row['id'] == 'T-20261025-04'
        cursor = 0
        for section in row['sections']:
            assert section['byte_start'] == cursor
            piece = source_raw[cursor:section['byte_end']]
            assert section['sha256'] == hashlib.sha256(piece).hexdigest()
            cursor = section['byte_end']
        assert cursor == len(source_raw)
    assert before == {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob('*')
        if path.is_file()
    }


def test_unknown_type_excluded_directories_close_each_open_handle(tmp_path, monkeypatch):
    directories = {".git", ".pytest_cache", "__pycache__", "SOLVED", "DONE", "_archive"}
    for name in directories:
        (tmp_path / name).mkdir()

    backend_type = _backend_type()
    original_open_directory = backend_type.open_directory
    original_close = backend_type.close
    opened = []
    closed = []

    def unknown_directory_type(self, _attributes):
        return None

    def recording_open_directory(self, parent, name):
        handle = original_open_directory(self, parent, name)
        if name in directories:
            opened.append(handle)
        return handle

    def recording_close(self, handle):
        closed.append(handle)
        return original_close(self, handle)

    monkeypatch.setattr(backend_type, "entry_is_directory", unknown_directory_type)
    monkeypatch.setattr(backend_type, "open_directory", recording_open_directory)
    monkeypatch.setattr(backend_type, "close", recording_close)

    report = capture_inventory(tmp_path)

    assert {row["path"] for row in report["excluded"]} == directories
    assert len(opened) == len(directories)
    assert all(handle in closed for handle in opened)
    assert report["counts"] == {"sources": 0, "tickets": 0, "errors": 0}


def test_default_omits_private_text_and_marks_opaque_sources(tmp_path):
    (tmp_path / 'note.txt').write_text('private-body', encoding='utf-8')
    (tmp_path / 'attachment.bin').write_bytes(b'\xff\x00')
    report = capture_inventory(tmp_path)
    assert 'private-body' not in json.dumps(report)
    opaque = next(row for row in report['sources'] if row['path'] == 'attachment.bin')
    assert opaque['encoding'] == 'opaque-needs-reader'
    assert opaque['semantic_review'] == 'pending'


def test_missing_root_and_unreadable_file_are_not_empty_success(tmp_path, monkeypatch):
    from lib import ticket_inventory

    assert capture_inventory(tmp_path / 'missing')['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    source = tmp_path / 'unreadable.txt'
    source.write_text('keep', encoding='utf-8')
    backend_type = _backend_type()
    original = backend_type.open_file

    def denied(self, parent, name):
        if name == source.name:
            raise ticket_inventory._TraversalProblem('file-unreadable')
        return original(self, parent, name)

    monkeypatch.setattr(backend_type, 'open_file', denied)
    report = capture_inventory(tmp_path)
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert report['errors'][0]['code'] == 'file-unreadable'


def test_links_are_reported_and_never_read(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private.txt').write_text('must-not-read', encoding='utf-8')
    queue = tmp_path / 'queue'
    queue.mkdir()
    _make_directory_link(queue / 'linked', outside)

    report = capture_inventory(queue, include_content=True)
    encoded = json.dumps(report)
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert not report['sources']
    assert 'private.txt' not in encoded
    assert 'must-not-read' not in encoded
    assert capture_inventory(queue / 'linked')['state'] == 'INCOMPLETE_CAPTURE_HOLD'


@pytest.mark.skipif(os.name != 'nt', reason='native Windows junction race')
def test_transient_windows_junction_before_relative_open_is_not_followed(tmp_path, monkeypatch):
    from lib import ticket_inventory

    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('outside-only-secret', encoding='utf-8')
    queue = tmp_path / 'queue'
    queue.mkdir()
    active = queue / 'active'
    active.mkdir()
    (active / 'inside.txt').write_text('inside-queue', encoding='utf-8')
    backup = queue / 'active.saved'
    backend_type = ticket_inventory._WindowsHandles
    original = backend_type.open_directory
    swapped = False

    def exchange(self, parent, name):
        nonlocal swapped
        if name == 'active' and not swapped:
            swapped = True
            os.rename(active, backup)
            try:
                _make_directory_link(active, outside)
                try:
                    return original(self, parent, name)
                finally:
                    _remove_directory_link(active)
            finally:
                os.rename(backup, active)
        return original(self, parent, name)

    monkeypatch.setattr(backend_type, 'open_directory', exchange)
    report = capture_inventory(queue, include_content=True)
    encoded = json.dumps(report)
    assert swapped
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert 'secret.txt' not in encoded
    assert 'outside-only-secret' not in encoded
    assert any(error['code'] == 'link-or-reparse-not-followed' for error in report['errors'])


@pytest.mark.skipif(os.name != 'nt', reason='native Windows junction race')
def test_windows_handle_stays_on_open_directory_during_transient_junction(tmp_path, monkeypatch):
    from lib import ticket_inventory

    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('outside-only-secret', encoding='utf-8')
    queue = tmp_path / 'queue'
    queue.mkdir()
    active = queue / 'active'
    active.mkdir()
    (active / 'inside.txt').write_text('inside-queue', encoding='utf-8')
    backup = queue / 'active.saved'
    backend_type = ticket_inventory._WindowsHandles
    original = backend_type.list_entries
    calls = 0

    def exchange(self, directory):
        nonlocal calls
        calls += 1
        if calls == 2:
            os.rename(active, backup)
            try:
                _make_directory_link(active, outside)
                try:
                    return original(self, directory)
                finally:
                    _remove_directory_link(active)
            finally:
                os.rename(backup, active)
        return original(self, directory)

    monkeypatch.setattr(backend_type, 'list_entries', exchange)
    report = capture_inventory(queue, include_content=True)
    encoded = json.dumps(report)
    assert calls >= 2
    assert report['state'] == 'SEMANTIC_REVIEW_REQUIRED'
    assert any(row['path'] == 'active/inside.txt' for row in report['sources'])
    assert 'inside-queue' in encoded
    assert 'secret.txt' not in encoded
    assert 'outside-only-secret' not in encoded


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symbolic-link race')
def test_transient_posix_symlink_before_file_open_is_not_followed(tmp_path, monkeypatch):
    from lib import ticket_inventory

    outside = tmp_path / 'outside-secret.txt'
    outside.write_text('outside-only-secret', encoding='utf-8')
    queue = tmp_path / 'queue'
    queue.mkdir()
    task = queue / 'task.txt'
    task.write_text('inside-queue', encoding='utf-8')
    backup = queue / 'task.saved'
    backend_type = ticket_inventory._PosixHandles
    original = backend_type.open_file
    swapped = False

    def exchange(self, parent, name):
        nonlocal swapped
        if name == 'task.txt' and not swapped:
            swapped = True
            os.rename(task, backup)
            try:
                task.symlink_to(outside)
                try:
                    return original(self, parent, name)
                finally:
                    task.unlink()
            finally:
                os.rename(backup, task)
        return original(self, parent, name)

    monkeypatch.setattr(backend_type, 'open_file', exchange)
    report = capture_inventory(queue, include_content=True)
    encoded = json.dumps(report)
    assert swapped
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert 'outside-secret.txt' not in encoded
    assert 'outside-only-secret' not in encoded
    assert any(error['code'] == 'link-or-reparse-not-followed' for error in report['errors'])


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symbolic-link race')
def test_transient_posix_symlink_before_relative_directory_open_is_not_followed(tmp_path, monkeypatch):
    from lib import ticket_inventory

    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('outside-only-secret', encoding='utf-8')
    queue = tmp_path / 'queue'
    queue.mkdir()
    active = queue / 'active'
    active.mkdir()
    (active / 'inside.txt').write_text('inside-queue', encoding='utf-8')
    backup = queue / 'active.saved'
    backend_type = ticket_inventory._PosixHandles
    original = backend_type.open_directory
    swapped = False

    def exchange(self, parent, name):
        nonlocal swapped
        if name == 'active' and not swapped:
            swapped = True
            os.rename(active, backup)
            try:
                active.symlink_to(outside, target_is_directory=True)
                try:
                    return original(self, parent, name)
                finally:
                    active.unlink()
            finally:
                os.rename(backup, active)
        return original(self, parent, name)

    monkeypatch.setattr(backend_type, 'open_directory', exchange)
    report = capture_inventory(queue, include_content=True)
    encoded = json.dumps(report)
    assert swapped
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert 'secret.txt' not in encoded
    assert 'outside-only-secret' not in encoded
    assert report['errors']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX ancestor symbolic-link race')
def test_posix_ancestor_symlink_exchange_before_queue_open_is_rejected(tmp_path, monkeypatch):
    from lib import ticket_inventory

    ancestor = tmp_path / 'anchor'
    ancestor.mkdir()
    queue = ancestor / 'queue'
    queue.mkdir()
    (queue / 'inside.txt').write_text('inside-queue', encoding='utf-8')
    outside = tmp_path / 'outside'
    outside.mkdir()
    external_queue = outside / 'queue'
    external_queue.mkdir()
    (external_queue / 'secret.txt').write_text('outside-only-secret', encoding='utf-8')
    backup = tmp_path / 'anchor.saved'
    backend_type = ticket_inventory._PosixHandles
    original = backend_type.open_queue
    swapped = False

    def exchange(self, path):
        nonlocal swapped
        if path == queue and not swapped:
            swapped = True
            os.rename(ancestor, backup)
            try:
                ancestor.symlink_to(outside, target_is_directory=True)
                try:
                    return original(self, path)
                finally:
                    ancestor.unlink()
            finally:
                os.rename(backup, ancestor)
        return original(self, path)

    monkeypatch.setattr(backend_type, 'open_queue', exchange)
    report = capture_inventory(queue, include_content=True)
    encoded = json.dumps(report)
    assert swapped
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert 'secret.txt' not in encoded
    assert 'outside-only-secret' not in encoded
    assert report['errors']


def test_directory_membership_change_is_reported(tmp_path, monkeypatch):
    (tmp_path / 'first.txt').write_text('keep', encoding='utf-8')
    backend_type = _backend_type()
    original = backend_type.list_entries
    calls = 0

    def changing(self, directory):
        nonlocal calls
        calls += 1
        if calls == 2:
            (tmp_path / 'late.txt').write_text('new task', encoding='utf-8')
        return original(self, directory)

    monkeypatch.setattr(backend_type, 'list_entries', changing)
    report = capture_inventory(tmp_path)
    assert any(item['code'] == 'directory-changed-during-read' for item in report['errors'])
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'


def test_cli_alias_never_launches_provider(tmp_path, monkeypatch, capsys):
    module_path = Path(__file__).resolve().parents[1] / 'bin' / 'ticket_master.py'
    spec = importlib.util.spec_from_file_location('inventory_cli_test', module_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'launch_provider', lambda *args, **kwargs: pytest.fail('provider started'))
    (tmp_path / 'task.txt').write_text('open task', encoding='utf-8')
    assert cli.main(['--ticketinventur', '--tickets-dir', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'SEMANTIC_REVIEW_REQUIRED'
    assert cli.main(['--inventory-content']) == 2
    assert cli.main(['--ticket-inventory', '--tickets-dir', str(tmp_path / 'missing')]) == 2

    (tmp_path / '_TICKETS').mkdir()
    (tmp_path / 'TICKETS').mkdir()
    assert cli.main(['--ticket-inventory', '--tickets-dir', str(tmp_path / '_TICKETS')]) == 2
    captured = capsys.readouterr()
    assert 'both queue names exist side by side' in captured.err

@pytest.mark.skipif(os.name != 'nt', reason='native Windows ancestor junction race')
def test_windows_ancestor_junction_exchange_before_queue_open_is_rejected(tmp_path, monkeypatch):
    from lib import ticket_inventory

    outside = tmp_path.parent / 'outside-target'
    outside.mkdir()
    (outside / 'secret.txt').write_text('outside-only-secret', encoding='utf-8')
    queue = tmp_path / 'queue'
    queue.mkdir()
    (queue / 'inside.txt').write_text('inside-queue', encoding='utf-8')
    saved = tmp_path.with_name(tmp_path.name + '.saved')
    backend_type = ticket_inventory._WindowsHandles
    original = backend_type._open_relative
    swapped = False

    def exchange(self, parent, name, *, directory):
        nonlocal swapped
        if name == tmp_path.name and not swapped:
            swapped = True
            os.rename(tmp_path, saved)
            try:
                _make_directory_link(tmp_path, outside)
                try:
                    return original(self, parent, name, directory=directory)
                finally:
                    _remove_directory_link(tmp_path)
            finally:
                os.rename(saved, tmp_path)
        return original(self, parent, name, directory=directory)

    monkeypatch.setattr(backend_type, '_open_relative', exchange)
    report = capture_inventory(queue, include_content=True)
    encoded = json.dumps(report)
    assert swapped
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert 'secret.txt' not in encoded
    assert 'outside-only-secret' not in encoded
