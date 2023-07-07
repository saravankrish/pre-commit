from __future__ import annotations

import os.path
import shutil
from typing import Any
from unittest import mock

import cfgv
import pytest
import re_assert

import pre_commit.constants as C
from pre_commit import git
from pre_commit.clientlib import CONFIG_SCHEMA
from pre_commit.clientlib import load_manifest
from pre_commit.envcontext import envcontext
from pre_commit.hook import Hook
from pre_commit.languages import golang
from pre_commit.languages import helpers
from pre_commit.languages import python
from pre_commit.languages.all import languages
from pre_commit.prefix import Prefix
from pre_commit.repository import _hook_installed
from pre_commit.repository import all_hooks
from pre_commit.repository import install_hook_envs
from pre_commit.util import cmd_output
from pre_commit.util import cmd_output_b
from testing.fixtures import make_config_from_repo
from testing.fixtures import make_repo
from testing.fixtures import modify_manifest
from testing.util import cwd
from testing.util import get_resource_path


def _norm_out(b):
    return b.replace(b'\r\n', b'\n')


def _hook_run(hook, filenames, color):
    with languages[hook.language].in_env(hook.prefix, hook.language_version):
        return languages[hook.language].run_hook(
            hook.prefix,
            hook.entry,
            hook.args,
            filenames,
            is_local=hook.src == 'local',
            require_serial=hook.require_serial,
            color=color,
        )


def _get_hook_no_install(repo_config, store, hook_id):
    config = {'repos': [repo_config]}
    config = cfgv.validate(config, CONFIG_SCHEMA)
    config = cfgv.apply_defaults(config, CONFIG_SCHEMA)
    hooks = all_hooks(config, store)
    hook, = (hook for hook in hooks if hook.id == hook_id)
    return hook


def _get_hook(repo_config, store, hook_id):
    hook = _get_hook_no_install(repo_config, store, hook_id)
    install_hook_envs([hook], store)
    return hook


def _test_hook_repo(
        tempdir_factory,
        store,
        repo_path,
        hook_id,
        args,
        expected,
        expected_return_code=0,
        config_kwargs=None,
        color=False,
):
    path = make_repo(tempdir_factory, repo_path)
    config = make_config_from_repo(path, **(config_kwargs or {}))
    hook = _get_hook(config, store, hook_id)
    ret, out = _hook_run(hook, args, color=color)
    assert ret == expected_return_code
    assert _norm_out(out) == expected


def test_python_hook(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'python_hooks_repo',
        'foo', [os.devnull],
        f'[{os.devnull!r}]\nHello World\n'.encode(),
    )


def test_python_hook_default_version(tempdir_factory, store):
    # make sure that this continues to work for platforms where default
    # language detection does not work
    with mock.patch.object(
            python,
            'get_default_version',
            return_value=C.DEFAULT,
    ):
        test_python_hook(tempdir_factory, store)


def test_python_hook_args_with_spaces(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'python_hooks_repo',
        'foo',
        [],
        b"['i have spaces', 'and\"\\'quotes', '$and !this']\n"
        b'Hello World\n',
        config_kwargs={
            'hooks': [{
                'id': 'foo',
                'args': ['i have spaces', 'and"\'quotes', '$and !this'],
            }],
        },
    )


def test_python_hook_weird_setup_cfg(in_git_dir, tempdir_factory, store):
    in_git_dir.join('setup.cfg').write('[install]\ninstall_scripts=/usr/sbin')

    _test_hook_repo(
        tempdir_factory, store, 'python_hooks_repo',
        'foo', [os.devnull],
        f'[{os.devnull!r}]\nHello World\n'.encode(),
    )


def test_python_venv_deprecation(store, caplog):
    config = {
        'repo': 'local',
        'hooks': [{
            'id': 'example',
            'name': 'example',
            'language': 'python_venv',
            'entry': 'echo hi',
        }],
    }
    _get_hook(config, store, 'example')
    assert caplog.messages[-1] == (
        '`repo: local` uses deprecated `language: python_venv`.  '
        'This is an alias for `language: python`.  '
        'Often `pre-commit autoupdate --repo local` will fix this.'
    )


def test_language_versioned_python_hook(tempdir_factory, store):
    # we patch this force virtualenv executing with `-p` since we can't
    # reliably have multiple pythons available in CI
    with mock.patch.object(
            python,
            '_sys_executable_matches',
            return_value=False,
    ):
        _test_hook_repo(
            tempdir_factory, store, 'python3_hooks_repo',
            'python3-hook',
            [os.devnull],
            f'3\n[{os.devnull!r}]\nHello World\n'.encode(),
        )


def test_system_hook_with_spaces(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'system_hook_with_spaces_repo',
        'system-hook-with-spaces', [os.devnull], b'Hello World\n',
    )


def test_golang_system_hook(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'golang_hooks_repo',
        'golang-hook', ['system'], b'hello world from system\n',
        config_kwargs={
            'hooks': [{
                'id': 'golang-hook',
                'language_version': 'system',
            }],
        },
    )


def test_golang_versioned_hook(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'golang_hooks_repo',
        'golang-hook', [], b'hello world from go1.18.4\n',
        config_kwargs={
            'hooks': [{
                'id': 'golang-hook',
                'language_version': '1.18.4',
            }],
        },
    )


def test_golang_hook_still_works_when_gobin_is_set(tempdir_factory, store):
    gobin_dir = tempdir_factory.get()
    with envcontext((('GOBIN', gobin_dir),)):
        test_golang_system_hook(tempdir_factory, store)
    assert os.listdir(gobin_dir) == []


def test_golang_with_recursive_submodule(tmpdir, tempdir_factory, store):
    sub_go = '''\
package sub

import "fmt"

func Func() {
    fmt.Println("hello hello world")
}
'''
    sub = tmpdir.join('sub').ensure_dir()
    sub.join('sub.go').write(sub_go)
    cmd_output('git', '-C', str(sub), 'init', '.')
    cmd_output('git', '-C', str(sub), 'add', '.')
    git.commit(str(sub))

    pre_commit_hooks = '''\
-   id: example
    name: example
    entry: example
    language: golang
    verbose: true
'''
    go_mod = '''\
module github.com/asottile/example

go 1.14
'''
    main_go = '''\
package main

import "github.com/asottile/example/sub"

func main() {
    sub.Func()
}
'''
    repo = tmpdir.join('repo').ensure_dir()
    repo.join('.pre-commit-hooks.yaml').write(pre_commit_hooks)
    repo.join('go.mod').write(go_mod)
    repo.join('main.go').write(main_go)
    cmd_output('git', '-C', str(repo), 'init', '.')
    cmd_output('git', '-C', str(repo), 'add', '.')
    cmd_output('git', '-C', str(repo), 'submodule', 'add', str(sub), 'sub')
    git.commit(str(repo))

    config = make_config_from_repo(str(repo))
    hook = _get_hook(config, store, 'example')
    ret, out = _hook_run(hook, (), color=False)
    assert ret == 0
    assert _norm_out(out) == b'hello hello world\n'


def test_missing_executable(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'not_found_exe',
        'not-found-exe', [os.devnull],
        b'Executable `i-dont-exist-lol` not found',
        expected_return_code=1,
    )


def test_run_a_script_hook(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'script_hooks_repo',
        'bash_hook', ['bar'], b'bar\nHello World\n',
    )


def test_run_hook_with_spaced_args(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'arg_per_line_hooks_repo',
        'arg-per-line',
        ['foo bar', 'baz'],
        b'arg: hello\narg: world\narg: foo bar\narg: baz\n',
    )


def test_run_hook_with_curly_braced_arguments(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'arg_per_line_hooks_repo',
        'arg-per-line',
        [],
        b"arg: hi {1}\narg: I'm {a} problem\n",
        config_kwargs={
            'hooks': [{
                'id': 'arg-per-line',
                'args': ['hi {1}', "I'm {a} problem"],
            }],
        },
    )


def test_intermixed_stdout_stderr(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'stdout_stderr_repo',
        'stdout-stderr',
        [],
        b'0\n1\n2\n3\n4\n5\n',
    )


@pytest.mark.xfail(os.name == 'nt', reason='ptys are posix-only')
def test_output_isatty(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'stdout_stderr_repo',
        'tty-check',
        [],
        b'stdin: False\nstdout: True\nstderr: True\n',
        color=True,
    )


def _make_grep_repo(entry, store, args=()):
    config = {
        'repo': 'local',
        'hooks': [{
            'id': 'grep-hook',
            'name': 'grep-hook',
            'language': 'pygrep',
            'entry': entry,
            'args': args,
            'types': ['text'],
        }],
    }
    return _get_hook(config, store, 'grep-hook')


@pytest.fixture
def greppable_files(tmpdir):
    with tmpdir.as_cwd():
        cmd_output_b('git', 'init', '.')
        tmpdir.join('f1').write_binary(b"hello'hi\nworld\n")
        tmpdir.join('f2').write_binary(b'foo\nbar\nbaz\n')
        tmpdir.join('f3').write_binary(b'[WARN] hi\n')
        yield tmpdir


def test_grep_hook_matching(greppable_files, store):
    hook = _make_grep_repo('ello', store)
    ret, out = _hook_run(hook, ('f1', 'f2', 'f3'), color=False)
    assert ret == 1
    assert _norm_out(out) == b"f1:1:hello'hi\n"


def test_grep_hook_case_insensitive(greppable_files, store):
    hook = _make_grep_repo('ELLO', store, args=['-i'])
    ret, out = _hook_run(hook, ('f1', 'f2', 'f3'), color=False)
    assert ret == 1
    assert _norm_out(out) == b"f1:1:hello'hi\n"


@pytest.mark.parametrize('regex', ('nope', "foo'bar", r'^\[INFO\]'))
def test_grep_hook_not_matching(regex, greppable_files, store):
    hook = _make_grep_repo(regex, store)
    ret, out = _hook_run(hook, ('f1', 'f2', 'f3'), color=False)
    assert (ret, out) == (0, b'')


def _norm_pwd(path):
    # Under windows bash's temp and windows temp is different.
    # This normalizes to the bash /tmp
    return cmd_output_b(
        'bash', '-c', f"cd '{path}' && pwd",
    )[1].strip()


def test_cwd_of_hook(in_git_dir, tempdir_factory, store):
    # Note: this doubles as a test for `system` hooks
    _test_hook_repo(
        tempdir_factory, store, 'prints_cwd_repo',
        'prints_cwd', ['-L'], _norm_pwd(in_git_dir.strpath) + b'\n',
    )


def test_lots_of_files(tempdir_factory, store):
    _test_hook_repo(
        tempdir_factory, store, 'script_hooks_repo',
        'bash_hook', [os.devnull] * 15000, mock.ANY,
    )


def test_additional_dependencies_roll_forward(tempdir_factory, store):
    path = make_repo(tempdir_factory, 'python_hooks_repo')

    config1 = make_config_from_repo(path)
    hook1 = _get_hook(config1, store, 'foo')
    with python.in_env(hook1.prefix, hook1.language_version):
        assert 'mccabe' not in cmd_output('pip', 'freeze', '-l')[1]

    # Make another repo with additional dependencies
    config2 = make_config_from_repo(path)
    config2['hooks'][0]['additional_dependencies'] = ['mccabe']
    hook2 = _get_hook(config2, store, 'foo')
    with python.in_env(hook2.prefix, hook2.language_version):
        assert 'mccabe' in cmd_output('pip', 'freeze', '-l')[1]

    # should not have affected original
    with python.in_env(hook1.prefix, hook1.language_version):
        assert 'mccabe' not in cmd_output('pip', 'freeze', '-l')[1]


@pytest.mark.parametrize('v', ('v1', 'v2'))
def test_repository_state_compatibility(tempdir_factory, store, v):
    path = make_repo(tempdir_factory, 'python_hooks_repo')

    config = make_config_from_repo(path)
    hook = _get_hook(config, store, 'foo')
    envdir = helpers.environment_dir(
        hook.prefix,
        python.ENVIRONMENT_DIR,
        hook.language_version,
    )
    os.remove(os.path.join(envdir, f'.install_state_{v}'))
    assert _hook_installed(hook) is True


def test_additional_golang_dependencies_installed(
        tempdir_factory, store,
):
    path = make_repo(tempdir_factory, 'golang_hooks_repo')
    config = make_config_from_repo(path)
    # A small go package
    deps = ['golang.org/x/example/hello@latest']
    config['hooks'][0]['additional_dependencies'] = deps
    hook = _get_hook(config, store, 'golang-hook')
    envdir = helpers.environment_dir(
        hook.prefix,
        golang.ENVIRONMENT_DIR,
        golang.get_default_version(),
    )
    binaries = os.listdir(os.path.join(envdir, 'bin'))
    # normalize for windows
    binaries = [os.path.splitext(binary)[0] for binary in binaries]
    assert 'hello' in binaries


def test_local_golang_additional_dependencies(store):
    config = {
        'repo': 'local',
        'hooks': [{
            'id': 'hello',
            'name': 'hello',
            'entry': 'hello',
            'language': 'golang',
            'additional_dependencies': ['golang.org/x/example/hello@latest'],
        }],
    }
    hook = _get_hook(config, store, 'hello')
    ret, out = _hook_run(hook, (), color=False)
    assert ret == 0
    assert _norm_out(out) == b'Hello, Go examples!\n'


def test_fail_hooks(store):
    config = {
        'repo': 'local',
        'hooks': [{
            'id': 'fail',
            'name': 'fail',
            'language': 'fail',
            'entry': 'make sure to name changelogs as .rst!',
            'files': r'changelog/.*(?<!\.rst)$',
        }],
    }
    hook = _get_hook(config, store, 'fail')
    ret, out = _hook_run(
        hook, ('changelog/123.bugfix', 'changelog/wat'), color=False,
    )
    assert ret == 1
    assert out == (
        b'make sure to name changelogs as .rst!\n'
        b'\n'
        b'changelog/123.bugfix\n'
        b'changelog/wat\n'
    )


def test_unknown_keys(store, caplog):
    config = {
        'repo': 'local',
        'hooks': [{
            'id': 'too-much',
            'name': 'too much',
            'hello': 'world',
            'foo': 'bar',
            'language': 'system',
            'entry': 'true',
        }],
    }
    _get_hook(config, store, 'too-much')
    msg, = caplog.messages
    assert msg == 'Unexpected key(s) present on local => too-much: foo, hello'


def test_reinstall(tempdir_factory, store, log_info_mock):
    path = make_repo(tempdir_factory, 'python_hooks_repo')
    config = make_config_from_repo(path)
    _get_hook(config, store, 'foo')
    # We print some logging during clone (1) + install (3)
    assert log_info_mock.call_count == 4
    log_info_mock.reset_mock()
    # Reinstall on another run should not trigger another install
    _get_hook(config, store, 'foo')
    assert log_info_mock.call_count == 0


def test_control_c_control_c_on_install(tempdir_factory, store):
    """Regression test for #186."""
    path = make_repo(tempdir_factory, 'python_hooks_repo')
    config = make_config_from_repo(path)
    hooks = [_get_hook_no_install(config, store, 'foo')]

    class MyKeyboardInterrupt(KeyboardInterrupt):
        pass

    # To simulate a killed install, we'll make PythonEnv.run raise ^C
    # and then to simulate a second ^C during cleanup, we'll make shutil.rmtree
    # raise as well.
    with pytest.raises(MyKeyboardInterrupt):
        with mock.patch.object(
            helpers, 'run_setup_cmd', side_effect=MyKeyboardInterrupt,
        ):
            with mock.patch.object(
                shutil, 'rmtree', side_effect=MyKeyboardInterrupt,
            ):
                install_hook_envs(hooks, store)

    # Should have made an environment, however this environment is broken!
    hook, = hooks
    envdir = helpers.environment_dir(
        hook.prefix,
        python.ENVIRONMENT_DIR,
        hook.language_version,
    )

    assert os.path.exists(envdir)

    # However, it should be perfectly runnable (reinstall after botched
    # install)
    install_hook_envs(hooks, store)
    ret, out = _hook_run(hook, (), color=False)
    assert ret == 0


def test_invalidated_virtualenv(tempdir_factory, store):
    # A cached virtualenv may become invalidated if the system python upgrades
    # This should not cause every hook in that virtualenv to fail.
    path = make_repo(tempdir_factory, 'python_hooks_repo')
    config = make_config_from_repo(path)
    hook = _get_hook(config, store, 'foo')

    # Simulate breaking of the virtualenv
    envdir = helpers.environment_dir(
        hook.prefix,
        python.ENVIRONMENT_DIR,
        hook.language_version,
    )
    libdir = os.path.join(envdir, 'lib', hook.language_version)
    paths = [
        os.path.join(libdir, p) for p in ('site.py', 'site.pyc', '__pycache__')
    ]
    cmd_output_b('rm', '-rf', *paths)

    # pre-commit should rebuild the virtualenv and it should be runnable
    hook = _get_hook(config, store, 'foo')
    ret, out = _hook_run(hook, (), color=False)
    assert ret == 0


def test_really_long_file_paths(tempdir_factory, store):
    base_path = tempdir_factory.get()
    really_long_path = os.path.join(base_path, 'really_long' * 10)
    cmd_output_b('git', 'init', really_long_path)

    path = make_repo(tempdir_factory, 'python_hooks_repo')
    config = make_config_from_repo(path)

    with cwd(really_long_path):
        _get_hook(config, store, 'foo')


def test_config_overrides_repo_specifics(tempdir_factory, store):
    path = make_repo(tempdir_factory, 'script_hooks_repo')
    config = make_config_from_repo(path)

    hook = _get_hook(config, store, 'bash_hook')
    assert hook.files == ''
    # Set the file regex to something else
    config['hooks'][0]['files'] = '\\.sh$'
    hook = _get_hook(config, store, 'bash_hook')
    assert hook.files == '\\.sh$'


def _create_repo_with_tags(tempdir_factory, src, tag):
    path = make_repo(tempdir_factory, src)
    cmd_output_b('git', 'tag', tag, cwd=path)
    return path


def test_tags_on_repositories(in_tmpdir, tempdir_factory, store):
    tag = 'v1.1'
    git1 = _create_repo_with_tags(tempdir_factory, 'prints_cwd_repo', tag)
    git2 = _create_repo_with_tags(tempdir_factory, 'script_hooks_repo', tag)

    config1 = make_config_from_repo(git1, rev=tag)
    hook1 = _get_hook(config1, store, 'prints_cwd')
    ret1, out1 = _hook_run(hook1, ('-L',), color=False)
    assert ret1 == 0
    assert out1.strip() == _norm_pwd(in_tmpdir)

    config2 = make_config_from_repo(git2, rev=tag)
    hook2 = _get_hook(config2, store, 'bash_hook')
    ret2, out2 = _hook_run(hook2, ('bar',), color=False)
    assert ret2 == 0
    assert out2 == b'bar\nHello World\n'


@pytest.fixture
def local_python_config():
    # Make a "local" hooks repo that just installs our other hooks repo
    repo_path = get_resource_path('python_hooks_repo')
    manifest = load_manifest(os.path.join(repo_path, C.MANIFEST_FILE))
    hooks = [
        dict(hook, additional_dependencies=[repo_path]) for hook in manifest
    ]
    return {'repo': 'local', 'hooks': hooks}


def test_local_python_repo(store, local_python_config):
    hook = _get_hook(local_python_config, store, 'foo')
    # language_version should have been adjusted to the interpreter version
    assert hook.language_version != C.DEFAULT
    ret, out = _hook_run(hook, ('filename',), color=False)
    assert ret == 0
    assert _norm_out(out) == b"['filename']\nHello World\n"


def test_default_language_version(store, local_python_config):
    config: dict[str, Any] = {
        'default_language_version': {'python': 'fake'},
        'default_stages': ['commit'],
        'repos': [local_python_config],
    }

    # `language_version` was not set, should default
    hook, = all_hooks(config, store)
    assert hook.language_version == 'fake'

    # `language_version` is set, should not default
    config['repos'][0]['hooks'][0]['language_version'] = 'fake2'
    hook, = all_hooks(config, store)
    assert hook.language_version == 'fake2'


def test_default_stages(store, local_python_config):
    config: dict[str, Any] = {
        'default_language_version': {'python': C.DEFAULT},
        'default_stages': ['commit'],
        'repos': [local_python_config],
    }

    # `stages` was not set, should default
    hook, = all_hooks(config, store)
    assert hook.stages == ['commit']

    # `stages` is set, should not default
    config['repos'][0]['hooks'][0]['stages'] = ['push']
    hook, = all_hooks(config, store)
    assert hook.stages == ['push']


def test_hook_id_not_present(tempdir_factory, store, caplog):
    path = make_repo(tempdir_factory, 'script_hooks_repo')
    config = make_config_from_repo(path)
    config['hooks'][0]['id'] = 'i-dont-exist'
    with pytest.raises(SystemExit):
        _get_hook(config, store, 'i-dont-exist')
    _, msg = caplog.messages
    assert msg == (
        f'`i-dont-exist` is not present in repository file://{path}.  '
        f'Typo? Perhaps it is introduced in a newer version?  '
        f'Often `pre-commit autoupdate` fixes this.'
    )


def test_too_new_version(tempdir_factory, store, caplog):
    path = make_repo(tempdir_factory, 'script_hooks_repo')
    with modify_manifest(path) as manifest:
        manifest[0]['minimum_pre_commit_version'] = '999.0.0'
    config = make_config_from_repo(path)
    with pytest.raises(SystemExit):
        _get_hook(config, store, 'bash_hook')
    _, msg = caplog.messages
    pattern = re_assert.Matches(
        r'^The hook `bash_hook` requires pre-commit version 999\.0\.0 but '
        r'version \d+\.\d+\.\d+ is installed.  '
        r'Perhaps run `pip install --upgrade pre-commit`\.$',
    )
    pattern.assert_matches(msg)


@pytest.mark.parametrize('version', ('0.1.0', C.VERSION))
def test_versions_ok(tempdir_factory, store, version):
    path = make_repo(tempdir_factory, 'script_hooks_repo')
    with modify_manifest(path) as manifest:
        manifest[0]['minimum_pre_commit_version'] = version
    config = make_config_from_repo(path)
    # Should succeed
    _get_hook(config, store, 'bash_hook')


def test_manifest_hooks(tempdir_factory, store):
    path = make_repo(tempdir_factory, 'script_hooks_repo')
    config = make_config_from_repo(path)
    hook = _get_hook(config, store, 'bash_hook')

    assert hook == Hook(
        src=f'file://{path}',
        prefix=Prefix(mock.ANY),
        additional_dependencies=[],
        alias='',
        always_run=False,
        args=[],
        description='',
        entry='bin/hook.sh',
        exclude='^$',
        exclude_types=[],
        files='',
        id='bash_hook',
        language='script',
        language_version='default',
        log_file='',
        minimum_pre_commit_version='0',
        name='Bash hook',
        pass_filenames=True,
        require_serial=False,
        stages=(
            'commit', 'merge-commit', 'prepare-commit-msg', 'commit-msg',
            'post-commit', 'manual', 'post-checkout', 'push', 'post-merge',
            'post-rewrite',
        ),
        types=['file'],
        types_or=[],
        verbose=False,
        fail_fast=False,
    )


@pytest.mark.parametrize(
    'repo',
    (
        'dotnet_hooks_csproj_repo',
        'dotnet_hooks_sln_repo',
        'dotnet_hooks_combo_repo',
        'dotnet_hooks_csproj_prefix_repo',
    ),
)
def test_dotnet_hook(tempdir_factory, store, repo):
    _test_hook_repo(
        tempdir_factory, store, repo,
        'dotnet-example-hook', [], b'Hello from dotnet!\n',
    )


def test_non_installable_hook_error_for_language_version(store, caplog):
    config = {
        'repo': 'local',
        'hooks': [{
            'id': 'system-hook',
            'name': 'system-hook',
            'language': 'system',
            'entry': 'python3 -c "import sys; print(sys.version)"',
            'language_version': 'python3.10',
        }],
    }
    with pytest.raises(SystemExit) as excinfo:
        _get_hook(config, store, 'system-hook')
    assert excinfo.value.code == 1

    msg, = caplog.messages
    assert msg == (
        'The hook `system-hook` specifies `language_version` but is using '
        'language `system` which does not install an environment.  '
        'Perhaps you meant to use a specific language?'
    )


def test_non_installable_hook_error_for_additional_dependencies(store, caplog):
    config = {
        'repo': 'local',
        'hooks': [{
            'id': 'system-hook',
            'name': 'system-hook',
            'language': 'system',
            'entry': 'python3 -c "import sys; print(sys.version)"',
            'additional_dependencies': ['astpretty'],
        }],
    }
    with pytest.raises(SystemExit) as excinfo:
        _get_hook(config, store, 'system-hook')
    assert excinfo.value.code == 1

    msg, = caplog.messages
    assert msg == (
        'The hook `system-hook` specifies `additional_dependencies` but is '
        'using language `system` which does not install an environment.  '
        'Perhaps you meant to use a specific language?'
    )
