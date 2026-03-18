import os as _os

IGNORE_FILENAMES = [
        "_weakref.py",
        "threading.py",
        "atexit.py",
        "<string>",
    ]

IGNORE_DIRS = [
        "lib/python",
        "site-packages",
]

IGNORE_FUNCTION_NAMES = [
        "_shutdown",
        "_cleanup",
]

# pyttd's own package directory — filter out recorder.py, runner.py, cli.py, etc.
_pyttd_pkg_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))) + _os.sep

IGNORE_PATTERNS = IGNORE_DIRS + [_pyttd_pkg_dir] + IGNORE_FILENAMES + IGNORE_FUNCTION_NAMES
