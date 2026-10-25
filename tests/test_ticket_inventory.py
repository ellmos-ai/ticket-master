import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from lib.ticket_inventory import capture_inventory


def test_complete_source_coverage_and_exact_byte_sections(tmp_path):
    raw = b'\xef\xbb\xbfID: T-20261025-04\r\n\r\nUmlaut: ' + 'äöü'.encode() + b'\r\n'
    locations = ['T-20261025-01.txt', 'INBOX/T-20261025-04.txt',
                 'INBOX/_formalisiert/raw.txt', 'PENDING/legacy.txt',
                 '.USER/old.txt', '_ATTACHMENTS/note.md', 'unknown/relevant.md']
    for location in locations:
        path = tmp_path / location
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    for directory in ('SOLVED', 'DONE', '_archive', '_logs'):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'evidence.txt').write_text('historical', encoding='utf-8')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    report = capture_inventory(tmp_path, include_content=True)
    assert report['errors'] == []
    assert {row['path'] for row in report['sources']} == set(locations)
    assert {row['path'] for row in report['excluded']} == {'SOLVED', 'DONE', '_archive', '_logs'}
    assert not report['archive_go']
    assert report['state'] == 'SEMANTIC_REVIEW_REQUIRED'
    for row in report['sources']:
        assert row['sha256'] == hashlib.sha256(raw).hexdigest()
        assert row['id'] == 'T-20261025-04'  # preserve original ID, no padding rewrite
        cursor = 0
        for section in row['sections']:
            assert section['byte_start'] == cursor
            piece = raw[cursor:section['byte_end']]
            assert section['sha256'] == hashlib.sha256(piece).hexdigest()
            cursor = section['byte_end']
        assert cursor == len(raw)
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_default_omits_private_text_and_marks_opaque_sources(tmp_path):
    (tmp_path / 'note.txt').write_text('private-body', encoding='utf-8')
    (tmp_path / 'attachment.bin').write_bytes(b'\xff\x00')
    report = capture_inventory(tmp_path)
    assert 'private-body' not in json.dumps(report)
    opaque = next(row for row in report['sources'] if row['path'] == 'attachment.bin')
    assert opaque['encoding'] == 'opaque-needs-reader'
    assert opaque['semantic_review'] == 'pending'


def test_missing_root_and_unreadable_file_are_not_empty_success(tmp_path, monkeypatch):
    assert capture_inventory(tmp_path / 'missing')['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    source = tmp_path / 'unreadable.txt'
    source.write_text('keep', encoding='utf-8')
    original = Path.open
    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError('private diagnostics must not escape')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    report = capture_inventory(tmp_path)
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert report['errors'][0]['code'] == 'file-unreadable'
    assert 'private diagnostics' not in json.dumps(report)


def test_links_are_reported_and_never_read(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private.txt').write_text('must-not-read', encoding='utf-8')
    queue = tmp_path / 'queue'
    queue.mkdir()
    try:
        (queue / 'linked').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('native symlink privilege unavailable')
    report = capture_inventory(queue, include_content=True)
    assert report['state'] == 'INCOMPLETE_CAPTURE_HOLD'
    assert not report['sources']
    assert 'must-not-read' not in json.dumps(report)
    assert capture_inventory(queue / 'linked')['state'] == 'INCOMPLETE_CAPTURE_HOLD'


def test_windows_reparse_flag_is_refused(tmp_path, monkeypatch):
    from lib import ticket_inventory
    import stat
    from types import SimpleNamespace
    assert ticket_inventory._reparse(SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400))
    original = ticket_inventory._reparse
    root_inode = tmp_path.lstat().st_ino
    monkeypatch.setattr(ticket_inventory, '_reparse', lambda info: info.st_ino == root_inode or original(info))
    assert capture_inventory(tmp_path)['state'] == 'INCOMPLETE_CAPTURE_HOLD'


def test_directory_membership_change_is_reported(tmp_path, monkeypatch):
    from lib import ticket_inventory
    original = ticket_inventory.os.listdir
    calls = 0
    def changing(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            (tmp_path / 'late.txt').write_text('new task', encoding='utf-8')
        return original(path)
    monkeypatch.setattr(ticket_inventory.os, 'listdir', changing)
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
