from __future__ import annotations

import contextlib
import functools
import json
import os.path
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import ContextManager
from typing import Generator
from typing import IO
from typing import Protocol
from typing import Sequence

import pre_commit.constants as C
from pre_commit.envcontext import envcontext
from pre_commit.envcontext import PatchesT
from pre_commit.envcontext import Var
from pre_commit.languages import helpers
from pre_commit.prefix import Prefix
from pre_commit.util import cmd_output
from pre_commit.util import rmtree

ENVIRONMENT_DIR = 'golangenv'
health_check = helpers.basic_health_check
run_hook = helpers.basic_run_hook

_ARCH_ALIASES = {
    'x86_64': 'amd64',
    'i386': '386',
    'aarch64': 'arm64',
    'armv8': 'arm64',
    'armv7l': 'armv6l',
}
_ARCH = platform.machine().lower()
_ARCH = _ARCH_ALIASES.get(_ARCH, _ARCH)


class ExtractAll(Protocol):
    def extractall(self, path: str) -> None: ...


if sys.platform == 'win32':  # pragma: win32 cover
    _EXT = 'zip'

    def _open_archive(bio: IO[bytes]) -> ContextManager[ExtractAll]:
        return zipfile.ZipFile(bio)
else:  # pragma: win32 no cover
    _EXT = 'tar.gz'

    def _open_archive(bio: IO[bytes]) -> ContextManager[ExtractAll]:
        return tarfile.open(fileobj=bio)


@functools.lru_cache(maxsize=1)
def get_default_version() -> str:
    if helpers.exe_exists('go'):
        return 'system'
    else:
        return C.DEFAULT


def get_env_patch(venv: str, version: str) -> PatchesT:
    if version == 'system':
        return (
            ('PATH', (os.path.join(venv, 'bin'), os.pathsep, Var('PATH'))),
        )

    return (
        ('GOROOT', os.path.join(venv, '.go')),
        (
            'PATH', (
                os.path.join(venv, 'bin'), os.pathsep,
                os.path.join(venv, '.go', 'bin'), os.pathsep, Var('PATH'),
            ),
        ),
    )


@functools.lru_cache
def _infer_go_version(version: str) -> str:
    if version != C.DEFAULT:
        return version
    resp = urllib.request.urlopen('https://go.dev/dl/?mode=json')
    # TODO: 3.9+ .removeprefix('go')
    return json.load(resp)[0]['version'][2:]


def _get_url(version: str) -> str:
    os_name = platform.system().lower()
    version = _infer_go_version(version)
    return f'https://dl.google.com/go/go{version}.{os_name}-{_ARCH}.{_EXT}'


def _install_go(version: str, dest: str) -> None:
    try:
        resp = urllib.request.urlopen(_get_url(version))
    except urllib.error.HTTPError as e:  # pragma: no cover
        if e.code == 404:
            raise ValueError(
                f'Could not find a version matching your system requirements '
                f'(os={platform.system().lower()}; arch={_ARCH})',
            ) from e
        else:
            raise
    else:
        with tempfile.TemporaryFile() as f:
            shutil.copyfileobj(resp, f)
            f.seek(0)

            with _open_archive(f) as archive:
                archive.extractall(dest)
        shutil.move(os.path.join(dest, 'go'), os.path.join(dest, '.go'))


@contextlib.contextmanager
def in_env(prefix: Prefix, version: str) -> Generator[None, None, None]:
    envdir = helpers.environment_dir(prefix, ENVIRONMENT_DIR, version)
    with envcontext(get_env_patch(envdir, version)):
        yield


def install_environment(
        prefix: Prefix,
        version: str,
        additional_dependencies: Sequence[str],
) -> None:
    env_dir = helpers.environment_dir(prefix, ENVIRONMENT_DIR, version)

    if version != 'system':
        _install_go(version, env_dir)

    if sys.platform == 'cygwin':  # pragma: no cover
        gopath = cmd_output('cygpath', '-w', env_dir)[1].strip()
    else:
        gopath = env_dir

    env = dict(os.environ, GOPATH=gopath)
    env.pop('GOBIN', None)
    if version != 'system':
        env['GOROOT'] = os.path.join(env_dir, '.go')
        env['PATH'] = os.pathsep.join((
            os.path.join(env_dir, '.go', 'bin'), os.environ['PATH'],
        ))

    helpers.run_setup_cmd(prefix, ('go', 'install', './...'), env=env)
    for dependency in additional_dependencies:
        helpers.run_setup_cmd(prefix, ('go', 'install', dependency), env=env)

    # save some disk space -- we don't need this after installation
    pkgdir = os.path.join(env_dir, 'pkg')
    if os.path.exists(pkgdir):  # pragma: no branch (always true on windows?)
        rmtree(pkgdir)
