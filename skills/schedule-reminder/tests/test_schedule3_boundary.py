"""Synthetic publication-destination controls for S2-C01."""
import json
from pathlib import Path
import pytest
import private_data
import os
import subprocess
import offline_support
from make_fixtures import runtime_storage_case
F=json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))


@pytest.fixture
def native_storage(tmp_path, monkeypatch, request):
    case = runtime_storage_case(tmp_path/'native', offline_support.REAL_RUN,
                                versioned=getattr(request, 'param', True))
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in case['environment'].items():
        monkeypatch.setenv(key, value)
    real_boundary = getattr(offline_support, 'REAL_BOUNDARY_FACTORY', None)
    if real_boundary is not None:
        monkeypatch.setattr(private_data, '_shared_boundary', real_boundary)
    calls = []
    def local_and_visibility(argv, *args, **kwargs):
        calls.append(argv)
        if argv[0] == 'git':
            directory = Path(argv[argv.index('-C')+1]) if '-C' in argv else Path(kwargs['cwd'])
            assert directory.resolve().is_relative_to(tmp_path)
            return offline_support.REAL_RUN(argv, *args, **kwargs)
        assert argv[:3] == ['gh', 'repo', 'view'], 'unexpected transport in native fixture'
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            'nameWithOwner': argv[3], 'visibility': F['visibility'].get(argv[3], 'UNKNOWN')}), '')
    monkeypatch.setattr(subprocess, 'run', local_and_visibility)
    # The live visibility answer comes from the Guards kit (every gh account); this fixture
    # answers it synthetically at that seam, as it answers the plain gh call above.
    monkeypatch.setattr(private_data, '_github_visibility', lambda name: (calls.append(['gh', 'repo', 'view', name]) or json.dumps(
        {'nameWithOwner': name, 'visibility': F['visibility'].get(name, 'UNKNOWN')})))
    return {**case, 'calls': calls}


@pytest.mark.parametrize('native_storage', [False], indirect=True)
def test_native_uncommitted_companion_refuses_data_before_creation(native_storage):
    target = native_storage['repository']/'new'/'record.json'
    with pytest.raises(ValueError, match='[Rr]ead-only companion Git query failed|committed HEAD'):
        private_data.prepare_parent(target)
    assert not target.parent.exists()


def test_native_ignored_data_refuses_before_creation(native_storage):
    target = native_storage['repository']/'ignored'/'record.json'
    with pytest.raises(ValueError, match='ignored'):
        private_data.prepare_parent(target)
    assert not target.parent.exists()


def test_native_ignored_lock_is_allowed_only_for_eligible_owner(native_storage):
    root = native_storage['repository']
    target = root/'record.json.lock'
    with pytest.raises(ValueError):
        private_data.prove_private(target)
    with private_data.file_lock(target):
        assert target.is_file()
    blocked = root/'ignored'/'record.json.lock'
    with pytest.raises(ValueError):
        with private_data.file_lock(blocked):
            pytest.fail('ignored DATA owner admitted a coordination lock')
    assert not blocked.parent.exists()


def test_native_versionable_write_can_be_reopened(native_storage):
    target = native_storage['repository']/'pending'/'record.json'
    for mode in ('w', 'a'):
        with private_data.open_for_write(target, mode, encoding='utf-8') as stream:
            stream.write(native_storage['record'])
    assert target.read_text(encoding='utf-8') == native_storage['record'] * 2
    assert native_storage['git']('status', '--porcelain', '--', 'pending/record.json').startswith('??')
    proof = private_data.prove_private(target)
    assert proof['head'] == native_storage['git']('rev-parse', '--verify', 'HEAD')
    assert proof['eligibility'] == 'versionable-data'


@pytest.mark.parametrize('filename', ['record.process.lock', '.lifecycle.lock', '.enqueue.lock'])
def test_native_coordination_locks_require_versionable_owner(native_storage, filename):
    root = native_storage['repository']
    admitted = root/'pending'/filename
    with private_data.file_lock(admitted):
        assert admitted.is_file()
    with pytest.raises(ValueError, match='ignored'):
        with private_data.file_lock(root/'ignored'/filename):
            pytest.fail('ignored owner admitted')
    assert not (root/'ignored').exists()


@pytest.mark.parametrize('configuration', ['public-remote', 'raw-selector'])
def test_native_shared_route_refusal_precedes_live_visibility(native_storage, monkeypatch, configuration):
    if configuration == 'public-remote':
        native_storage['git']('remote', 'set-url', 'origin', F['public_remote'])
    else:
        native_storage['git']('config', 'remote.pushDefault', F['public_remote'])
    monkeypatch.setattr(private_data, '_query', lambda argv: pytest.fail('unproven route queried live visibility'))
    target = native_storage['repository']/'pending'/'record.json'
    with pytest.raises(ValueError):
        private_data.prepare_parent(target)
    assert not target.parent.exists()


@pytest.mark.parametrize('change', ['configuration', 'head', 'ignore'])
def test_native_changed_proof_during_visibility_preserves_record(native_storage, monkeypatch, change):
    root = native_storage['repository']
    target = root/'record.json'
    target.write_text(native_storage['record'], encoding='utf-8')
    query = private_data._query
    def changing_query(argv):
        response = query(argv)
        if change == 'configuration':
            native_storage['git']('config', 'core.filemode', 'false')
            native_storage['git']('config', 'core.syntheticproof', 'changed')
        elif change == 'head':
            tree = native_storage['git']('rev-parse', 'HEAD^{tree}')
            commit = native_storage['git']('commit-tree', tree, '-p', 'HEAD',
                                           input=native_storage['changed_commit_message'])
            native_storage['git']('update-ref', 'HEAD', commit)
        else:
            with (root/'.gitignore').open('a', encoding='utf-8') as stream:
                stream.write(native_storage['ignored_record_rule'])
        return response
    monkeypatch.setattr(private_data, '_query', changing_query)
    with pytest.raises(ValueError, match='changed|became ignored'):
        private_data.open_for_write(target, 'w', encoding='utf-8')
    assert target.read_text(encoding='utf-8') == native_storage['record']


def test_native_ignore_change_after_open_prevents_truncation(native_storage, monkeypatch):
    root = native_storage['repository']
    target = root/'record.json'
    target.write_text(native_storage['record'], encoding='utf-8')
    original_open = os.open
    def changed_open(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        if Path(path) == target:
            with (root/'.gitignore').open('a', encoding='utf-8') as stream:
                stream.write(native_storage['ignored_record_rule'])
        return descriptor
    monkeypatch.setattr(os, 'open', changed_open)
    with pytest.raises(ValueError, match='ignored'):
        private_data.open_for_write(target, 'w', encoding='utf-8')
    assert target.read_text(encoding='utf-8') == native_storage['record']


@pytest.mark.parametrize('outcome', ['PUBLIC', 'UNKNOWN', 'mismatch', 'unavailable'])
def test_native_live_denial_preserves_existing_record(native_storage, monkeypatch, outcome):
    target = native_storage['repository']/'record.json'
    target.write_text(native_storage['record'], encoding='utf-8')
    def query(argv):
        if outcome == 'unavailable':
            raise subprocess.TimeoutExpired(argv, 20)
        return json.dumps({'nameWithOwner': 'example-owner/public-data' if outcome == 'mismatch' else argv[3],
                           'visibility': 'PRIVATE' if outcome == 'mismatch' else outcome})
    monkeypatch.setattr(private_data, '_query', query)
    with pytest.raises(ValueError):
        private_data.open_for_write(target, 'w', encoding='utf-8')
    assert target.read_text(encoding='utf-8') == native_storage['record']


def test_native_missing_local_receipt_fails_before_live_visibility(native_storage, monkeypatch):
    (Path(native_storage['environment']['HOME'])/'.pii-guard/visibility.json').unlink()
    monkeypatch.setattr(private_data, '_query', lambda argv: pytest.fail('missing local receipt queried live visibility'))
    target = native_storage['repository']/'pending'/'record.json'
    with pytest.raises(ValueError):
        private_data.prepare_parent(target)
    assert not target.parent.exists()

def setup_query(monkeypatch,tmp_path,mode):
    calls=[]
    boundary = offline_support.synthetic_boundary()
    def prove(destination):
        calls.append(('proof', str(destination)))
        if mode in {'push-public', 'alternate', 'ssh-override'}:
            raise boundary.GitError('synthetic shared proof refusal')
        from types import SimpleNamespace
        return SimpleNamespace(root=str(tmp_path), repositories=('example-owner/private-data',),
                               signature='synthetic-proof')
    boundary.prove_private_companion = prove
    monkeypatch.setattr(private_data, '_shared_boundary', lambda: boundary)
    def query(argv):
        calls.append(argv)
        if argv[0]=='gh':
            name=argv[3]
            return json.dumps({'nameWithOwner':name,'visibility':F['visibility'].get(name,'UNKNOWN')})
        pytest.fail('unexpected proof query')
    monkeypatch.setattr(private_data,'_query',query)
    return calls

@pytest.mark.parametrize('mode',['push-public','alternate','ssh-override'])
def test_every_publication_destination_must_be_private(tmp_path,monkeypatch,mode):
    setup_query(monkeypatch,tmp_path,mode)
    with pytest.raises(ValueError): private_data.prove_private(tmp_path/'data.json')

@pytest.mark.parametrize('name',['GIT_DIR','GIT_WORK_TREE','GIT_COMMON_DIR'])
def test_inherited_repository_selector_cannot_redirect_proof(tmp_path,monkeypatch,name):
    setup_query(monkeypatch,tmp_path,'https')
    monkeypatch.setenv(name,str(tmp_path/'synthetic-admin'))
    with pytest.raises(ValueError): private_data.prove_private(tmp_path/'data.json')

def test_https_private_control(tmp_path,monkeypatch):
    calls=setup_query(monkeypatch,tmp_path,'https')
    assert private_data.prove_private(tmp_path/'data.json')['visibility']=='PRIVATE'

def test_ssh_dependency_absence_fails_explicitly(tmp_path,monkeypatch):
    setup_query(monkeypatch,tmp_path,'ssh')
    monkeypatch.setattr(private_data,'SOURCE',tmp_path/'synthetic-tool')
    monkeypatch.setattr(private_data, '_shared_boundary', offline_support.REAL_BOUNDARY_FACTORY)
    with pytest.raises(ValueError,match='Guards.*dependency|dependency.*Guards'):
        private_data.prove_private(tmp_path/'data.json')


@pytest.mark.parametrize('name',['GIT_CONFIG','GIT_CONFIG_KEY_0','GIT_CONFIG_VALUE_0',
                                 'GIT_CONFIG_COUNT','GIT_CONFIG_PARAMETERS'])
@pytest.mark.parametrize('empty',[False,True])
def test_configuration_overrides_fail_before_any_proof_query(tmp_path,monkeypatch,name,empty):
    from make_fixtures import publication_environment_case
    case=publication_environment_case(tmp_path/'generated')
    calls=setup_query(monkeypatch,case['repository'],'https')
    monkeypatch.setenv(name,'' if empty else case['overrides'][name])
    with pytest.raises(ValueError):
        private_data.prove_private(case['repository']/'data.json')
    assert calls==[]


@pytest.mark.parametrize('result',['PUBLIC','UNKNOWN',None,0])
def test_live_visibility_requires_explicit_private(tmp_path,monkeypatch,result):
    setup_query(monkeypatch,tmp_path,'ssh')
    monkeypatch.setattr(private_data, '_query', lambda argv: json.dumps({
        'nameWithOwner': argv[3], 'visibility': result}))
    with pytest.raises(ValueError):
        private_data.prove_private(tmp_path/'data.json')
