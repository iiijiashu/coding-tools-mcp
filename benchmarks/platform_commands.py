"""Small cross-platform command helpers shared by benchmark runners."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence


def is_windows(platform: str | None = None) -> bool:
    return (os.name if platform is None else platform) == "nt"


def quote_shell_argument(value: str, *, platform: str | None = None) -> str:
    if is_windows(platform):
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def join_shell_command(arguments: Sequence[str], *, platform: str | None = None) -> str:
    values = [str(argument) for argument in arguments]
    if is_windows(platform):
        return subprocess.list2cmdline(values)
    return shlex.join(values)


def split_command_arguments(command: str, *, platform: str | None = None) -> list[str]:
    if not is_windows(platform):
        return shlex.split(command)
    # This is only the compatibility path for MCP implementations that expose
    # argv instead of cmd.  posix=False preserves Windows backslashes; remove
    # one matching layer of quotes from the simple benchmark command grammar.
    tokens = shlex.split(command, posix=False)
    return [
        token[1:-1]
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
        else token
        for token in tokens
    ]


def render_process_command(
    template: str,
    values: Mapping[str, object],
    *,
    platform: str | None = None,
) -> str | list[str]:
    rendered = template.format(
        **{
            key: quote_shell_argument(str(value), platform=platform)
            for key, value in values.items()
        }
    )
    # On Windows, passing the rendered command line directly to CreateProcess
    # preserves drive separators.  POSIX Popen requires an argv sequence.
    return rendered if is_windows(platform) else shlex.split(rendered)


def outside_file_read_command(path: str, *, platform: str | None = None) -> str:
    windows = is_windows(platform)
    executable = "type" if windows else "cat"
    native_path = path.replace("/", "\\") if windows else path
    return join_shell_command([executable, native_path], platform=platform)


def native_echo_arguments(*, platform: str | None = None) -> list[str]:
    if is_windows(platform):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "echo", "ok"]
    return ["printf", "ok"]
