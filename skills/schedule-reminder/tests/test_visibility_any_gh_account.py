"""The live PRIVATE check must not depend on which gh account is ACTIVE.

With an active gh account that cannot see the private companion, a plain `gh repo view` refused
every proof with "PRIVATE repository proof unavailable" until the account was switched back.
The default visibility query now goes through the pinned Guards kit, which asks every stored gh
account. The end-to-end test runs the REAL kit against a synthetic gh (from the kit's own fixture
generator) whose active account cannot see the repository.
"""
import importlib.util
import json
import os
import subprocess

import pytest

import offline_support
import private_data

REPOSITORY = 'example-owner/private-data'
ARGV = ['gh', 'repo', 'view', REPOSITORY, '--json', 'nameWithOwner,visibility']


def kit_fixtures():
    path = offline_support.ROOT/'guards/tools/make_fixtures.py'
    spec = importlib.util.spec_from_file_location('_guards_make_fixtures', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def real_kit_with_switched_gh(tmp_path, monkeypatch):
    fixtures = kit_fixtures()
    stub = fixtures.make_gh_cli_stub(tmp_path, accounts=['example-owner', 'other-account'],
                                     active='other-account', sees={'example-owner': [REPOSITORY]},
                                     visibility={REPOSITORY: 'PRIVATE'})
    monkeypatch.setenv('PATH', str(stub['bin']))
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_HOST', 'GH_ENTERPRISE_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    # The offline harness intercepts any `gh` subprocess; this test needs the real one on the
    # private PATH, so it restores the interpreter's own subprocess.run for its duration.
    monkeypatch.setattr(subprocess, 'run', offline_support.ORIGINAL_RUN)
    monkeypatch.setattr(private_data, '_shared_boundary', offline_support.REAL_BOUNDARY_FACTORY)
    return fixtures, stub


def test_switched_active_account_still_proves_private(real_kit_with_switched_gh):
    fixtures, stub = real_kit_with_switched_gh
    answer = json.loads(private_data._query(list(ARGV)))
    assert answer == {'nameWithOwner': REPOSITORY, 'visibility': 'PRIVATE'}
    calls = fixtures.gh_stub_calls(stub)
    assert not [call for call in calls if call['argv'][:2] == ['auth', 'switch']]
    assert [call['credential'] for call in calls if call['argv'][:2] == ['repo', 'view']] == ['example-owner']


def test_plain_active_account_query_is_the_incident(real_kit_with_switched_gh):
    """Negative control: the pre-fix query (plain gh with the active account) refuses here."""
    fixtures, stub = real_kit_with_switched_gh
    environment = dict(os.environ, GH_HOST='github.com')
    result = offline_support.ORIGINAL_RUN([str(stub['launcher']), *ARGV[1:]], capture_output=True, text=True, env=environment,
                                          **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
    assert result.returncode != 0


@pytest.mark.parametrize('visibility', ['PUBLIC', 'INTERNAL'])
def test_non_private_answers_are_passed_through_for_the_caller_to_refuse(monkeypatch, visibility):
    class Kit:
        GitError = RuntimeError

        @staticmethod
        def query_github_visibility(name):
            return visibility
    monkeypatch.setattr(private_data, '_shared_boundary', lambda: Kit)
    assert json.loads(private_data._query(list(ARGV)))['visibility'] == visibility


def test_no_credential_can_see_it_refuses(monkeypatch):
    class Kit:
        class GitError(Exception):
            pass

        @staticmethod
        def query_github_visibility(name):
            raise Kit.GitError('no gh credential can see the repository')
    monkeypatch.setattr(private_data, '_shared_boundary', lambda: Kit)
    with pytest.raises(ValueError, match='PRIVATE repository proof unavailable'):
        private_data._query(list(ARGV))


def test_a_kit_without_the_api_refuses(monkeypatch):
    class Kit:
        GitError = RuntimeError
    monkeypatch.setattr(private_data, '_shared_boundary', lambda: Kit)
    with pytest.raises(ValueError, match='account-independent'):
        private_data._query(list(ARGV))
