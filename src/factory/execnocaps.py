"""Replace this process with a command after dropping every ambient capability.

The supervisor keeps CAP_SETUID, CAP_SETGID and CAP_KILL as *ambient*
capabilities so that it can start workers under another UID and kill them
again. ``Popen(user=...)`` changes the UID with setuid(2), but the kernel only
clears capabilities when a UID changes from or to 0. Between two non-zero
UIDs they survive, and the ambient set is also carried through execve, so a
worker started that way would still hold CAP_SETUID and could setuid(2) back to
the supervisor's UID and read its environment, which holds the API keys.

``run_agent`` therefore starts the worker as ``python execnocaps.py -- claude
...``. This wrapper already runs as the worker identity. It clears the ambient
set and execs the real command; with no ambient set and no file capabilities
the exec leaves the worker with an empty permitted set. It fails closed:
if the ambient set cannot be cleared and verified empty, nothing is executed
(exit 126). Standard library only (``ctypes`` for prctl), no output besides
one short error line.
"""

import ctypes
import os
import sys

PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4
EXIT_REFUSED = 126
EXIT_NOT_FOUND = 127


def ambient_set():
    """The ambient capability mask of this process, or None if it cannot be read."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("CapAmb:"):
                    return int(line.split(":", 1)[1].strip(), 16)
    except (OSError, ValueError):
        return None
    return None


def clear_ambient():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        number = ctypes.get_errno()
        raise OSError(number, f"prctl(PR_CAP_AMBIENT_CLEAR_ALL): {os.strerror(number)}")


def drop_ambient():
    """Clear the ambient set; raise OSError unless it is verifiably empty afterwards."""
    before = ambient_set()
    if before is None:
        if sys.platform == "linux":
            raise OSError("cannot read CapAmb from /proc/self/status")
        return  # no capability model to drop on other platforms
    if before:
        clear_ambient()
    if ambient_set():
        raise OSError("ambient capabilities remain after clearing")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        print("usage: execnocaps.py -- COMMAND [ARG...]", file=sys.stderr)
        return EXIT_REFUSED
    try:
        drop_ambient()
    except OSError as error:
        print(f"factory: refusing to start the worker: {error}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        os.execvp(args[0], args)
    except OSError as error:
        print(f"factory: cannot execute the worker command: {error.strerror}", file=sys.stderr)
        return EXIT_NOT_FOUND
    return 0  # unreachable: execvp only returns by raising


if __name__ == "__main__":
    sys.exit(main())
