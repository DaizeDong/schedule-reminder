"""A PRIVATE proof is reused within one process only while it can still be true."""
from pathlib import Path

import pytest

import private_data

PRIVATE = 'https://github.com/example-owner/private-data.git'
PUBLIC = 'https://github.com/example-owner/public-data.git'


@pytest.fixture
def companion(tmp_path, monkeypatch):
    """The autouse synthetic companion at tmp_path, with a fake clock and a counted live query."""
    private_data.clear_proof_memo()
    now = [1000.0]
    monkeypatch.setattr(private_data, '_clock', lambda: now[0])
    answers, queries = {}, []

    def query(argv):
        queries.append(argv[3])
        visibility = answers.get(argv[3], 'PRIVATE')
        return '{"nameWithOwner": "%s", "visibility": "%s"}' % (argv[3], visibility)
    monkeypatch.setattr(private_data, '_query', query)
    yield {'root': tmp_path, 'now': now, 'answers': answers, 'queries': queries}
    private_data.clear_proof_memo()


def remote(root, url):
    (root/'.git'/'config').write_text('[remote "origin"]\nurl = '+url, encoding='utf-8')


def test_unchanged_companion_is_proved_once_for_every_destination(companion):
    root = companion['root']
    first = private_data.prove_private(root/'data'/'db.sqlite3')
    assert private_data.prove_private(root/'data'/'db.sqlite3') == first
    other = private_data.prove_private(root/'data'/'todo-actions')
    assert other['root'] == first['root'] and other['path'] != first['path']
    assert private_data.prove_private(root/'runs'/'.lifecycle.lock', _transient_lock=True)['eligibility'] == 'transient-lock'
    assert companion['queries'] == ['example-owner/private-data']


def test_a_refusal_is_not_remembered(companion):
    root = companion['root']
    companion['answers']['example-owner/private-data'] = 'PUBLIC'
    for _ in range(2):
        with pytest.raises(ValueError, match='PRIVATE'):
            private_data.prove_private(root/'data.json')
    assert companion['queries'] == ['example-owner/private-data'] * 2
    companion['answers'].clear()
    assert private_data.prove_private(root/'data.json')['visibility'] == 'PRIVATE'
    assert len(companion['queries']) == 3


def test_the_reused_proof_expires(companion):
    root = companion['root']
    private_data.prove_private(root/'data.json')
    companion['now'][0] += private_data.PROOF_TTL_SECONDS - 1
    private_data.prove_private(root/'data.json')
    assert len(companion['queries']) == 1
    companion['now'][0] += 2
    private_data.prove_private(root/'data.json')
    assert len(companion['queries']) == 2
    # An expired entry that now fails to prove refuses; it is not served from the old answer.
    companion['now'][0] += private_data.PROOF_TTL_SECONDS + 1
    companion['answers']['example-owner/private-data'] = 'PUBLIC'
    with pytest.raises(ValueError, match='PRIVATE'):
        private_data.prove_private(root/'data.json')


def test_a_changed_remote_is_proved_again_and_refused(companion):
    root = companion['root']
    private_data.prove_private(root/'data.json')
    remote(root, PUBLIC)
    with pytest.raises(ValueError):
        private_data.prove_private(root/'data.json')
    remote(root, PRIVATE)
    private_data.prove_private(root/'data.json')
    assert len(companion['queries']) == 2


def test_a_moved_head_is_proved_again(companion):
    root = companion['root']
    (root/'.git'/'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
    (root/'.git'/'refs'/'heads').mkdir(parents=True)
    (root/'.git'/'refs'/'heads'/'main').write_text('1'*40+'\n', encoding='utf-8')
    private_data.prove_private(root/'data.json')
    private_data.prove_private(root/'data.json')
    assert len(companion['queries']) == 1
    (root/'.git'/'refs'/'heads'/'main').write_text('2'*40+'\n', encoding='utf-8')
    private_data.prove_private(root/'data.json')
    assert len(companion['queries']) == 2


def test_a_nested_repository_is_not_served_from_its_parent(companion):
    root = companion['root']
    private_data.prove_private(root/'data.json')
    nested = root/'vendor'/'other'
    (nested/'.git').mkdir(parents=True)
    (nested/'.git'/'config').write_text('[remote "origin"]\nurl = '+PUBLIC, encoding='utf-8')
    with pytest.raises(ValueError):
        private_data.prove_private(nested/'data.json')


def test_a_changed_environment_is_proved_again(companion, monkeypatch):
    root = companion['root']
    private_data.prove_private(root/'data.json')
    monkeypatch.setenv('GIT_SSH_COMMAND', 'synthetic-transport')
    private_data.prove_private(root/'data.json')
    assert len(companion['queries']) == 2
