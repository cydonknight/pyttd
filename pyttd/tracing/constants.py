IGNORE_FILENAMES = [
        "_weakref.py",
        "threading.py",
        "atexit.py",
    ]

IGNORE_DIRS = [
        "lib/python",
        "site-packages",
]

IGNORE_FUNCTION_NAMES = [
        "_shutdown",
        "_cleanup",
]

IGNORE_PATTERNS = IGNORE_DIRS + IGNORE_FILENAMES + IGNORE_FUNCTION_NAMES