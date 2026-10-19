from pathlib import Path
import os
import subprocess

import pytest
from code_context.service import CodeService


def repository(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'main.py').write_text('def ready():\n    return True\n')
    return source


def test_snapshot_content_identity_no_source_mutation_or_symlinks(tmp_path):
    source = repository(tmp_path)
    outside = tmp_path / 'private.txt'
    outside.write_text('private')
    (source / 'link.py').symlink_to(outside)
    service = CodeService(tmp_path / 'data', {'source': source})
    repo = {'root_id': 'source', 'relative_path': '.', 'revision': 'snapshot'}
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    revision = service.snapshot(repo, first)
    assert service.snapshot(repo, second) == revision
    assert not (first / 'link.py').exists()
    (source / 'main.py').write_text('def ready():\n    return False\n')
    assert 'True' in (first / 'main.py').read_text()
    third = tmp_path / 'third'
    third.mkdir()
    assert service.snapshot(repo, third) != revision
    assert not (source / '.codegraph').exists()


def test_git_snapshot_uses_commit_excludes_uncommitted_changes(tmp_path):
    source = repository(tmp_path)
    for args in [('init', '-q'), ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.com'),
                 ('add', '.'), ('commit', '-qm', 'source')]:
        subprocess.run(['git', '-C', str(source), *args], check=True)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    (source / 'main.py').write_text('dirty working tree')
    target = tmp_path / 'snapshot'
    target.mkdir()
    service = CodeService(tmp_path / 'data', {'source': source})
    assert service.snapshot({'root_id': 'source', 'relative_path': '.', 'revision': revision}, target) == revision
    assert 'return True' in (target / 'main.py').read_text()
    assert not (target / '.git').exists()


def test_discovery_bounded_and_path_scope(tmp_path):
    source = repository(tmp_path)
    (source / 'README.md').write_text('# Design\nready before start')
    service = CodeService(tmp_path / 'data', {'source': source}, ['a'])
    assert service.discover('source', max_files=1)['discovery_incomplete']
    assert service.discover('source')['architecture_status'] == 'unconfirmed'
    for root, path in [('source', '../'), ('missing', '.'), ('source', str(source))]:
        with pytest.raises(ValueError):
            service.source(root, path)
    (source / 'dirlink').symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        service.source('source', 'dirlink')
    with pytest.raises(ValueError, match='not_allowed'):
        service.status('b', 'v1', 'missing')
    with pytest.raises(ValueError, match='job_not_found'):
        service.status('a', 'v1', 'missing')


def test_data_cannot_be_recursively_snapshotted(tmp_path):
    source = repository(tmp_path)
    service = CodeService(source / 'data', {'source': source})
    with pytest.raises(ValueError, match='outside_source'):
        service.source('source', '.')
