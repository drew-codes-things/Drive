import ctypes
import os
import signal
import sys

PR_SET_CHILD_SUBREAPER = 36
SUBREAPER_ON = 1
SIGNALS_PYTHON_IGNORES = (signal.SIGPIPE, signal.SIGXFSZ)
STARTER_NOTE = "toolstart: "


def keep_orphans_as_children() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as error:
        sys.stderr.write(f"{STARTER_NOTE}orphans may escape the stop at the time limit ({error})\n")
        return
    if libc.prctl(PR_SET_CHILD_SUBREAPER, SUBREAPER_ON, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        sys.stderr.write(f"{STARTER_NOTE}orphans may escape the stop at the time limit ({os.strerror(error)})\n")


def restore_default_signals() -> None:
    for number in SIGNALS_PYTHON_IGNORES:
        signal.signal(number, signal.SIG_DFL)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"{STARTER_NOTE}no command to run")
    keep_orphans_as_children()
    restore_default_signals()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
