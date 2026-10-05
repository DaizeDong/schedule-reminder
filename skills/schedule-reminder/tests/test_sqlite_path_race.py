"""Generated unlink observations preserve SQLite admission and alias refusals."""
import os
from pathlib import Path

import pytest

import private_data
from make_fixtures import cases, sqlite_path_race_cases, sqlite_path_stat


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
@pytest.mark.parametrize('case', sqlite_path_race_cases(), ids=lambda case: case['name'])
def test_sqlite_sidecar_unlink_observations(tmp_path, monkeypatch, suffix, case):
    database = tmp_path/'synthetic.sqlite3'
    sidecar = Path(str(database) + suffix)
    original_lstat = Path.lstat
    observed = []

    def lstat(path, *args, **kwargs):
        if path != sidecar:
            return original_lstat(path, *args, **kwargs)
        assert len(observed) < 3, 'zero-link observations must not cause unbounded retries'
        index = min(len(observed), len(case['observations']) - 1)
        observation = case['observations'][index]
        observed.append(observation)
        if observation == 'missing':
            raise FileNotFoundError(sidecar)
        return sqlite_path_stat(observation)

    monkeypatch.setattr(Path, 'lstat', lstat)
    if case['allowed']:
        assert private_data.assert_sqlite_paths(database) == database
        assert observed[-1] in {'regular', 'missing'}
    else:
        with pytest.raises(ValueError, match='unproven filesystem alias'):
            private_data.assert_sqlite_paths(database)
        assert observed[-1] != 'regular'


@pytest.mark.parametrize('kind', ['unlinked', 'hardlink'])
def test_opened_inode_is_not_retried_or_truncated(tmp_path, monkeypatch, kind):
    target = tmp_path/'synthetic-record.txt'
    original = cases()['adapter_text']
    target.write_text(original, encoding='utf-8')
    monkeypatch.setattr(private_data, 'prepare_parent', lambda *args, **kwargs: None)
    monkeypatch.setattr(os, 'fstat', lambda descriptor: sqlite_path_stat(kind))

    with pytest.raises(ValueError, match='inode changed or has another hardlink'):
        private_data.open_for_write(target, 'w', encoding='utf-8')

    assert target.read_text(encoding='utf-8') == original
