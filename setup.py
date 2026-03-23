import sys
import platform
from setuptools import setup, Extension

if sys.version_info < (3, 12):
    raise SystemExit("pyttd requires Python 3.12 or later")

extra_compile_args = []
if platform.system() == "Windows":
    extra_compile_args.append("/std:c11")
    extra_compile_args.append("/experimental:c11atomics")

sources = [
    "ext/pyttd_native.c",
    "ext/recorder.c",
    "ext/ringbuf.c",
    "ext/checkpoint.c",
    "ext/checkpoint_store.c",
    "ext/replay.c",
    "ext/iohook.c",
    "ext/sqliteflush.c",
]

libraries = []
define_macros = []
if platform.system() == "Windows":
    # Bundle the SQLite amalgamation on Windows (no system libsqlite3)
    sources.append("ext/sqlite3.c")
    define_macros.append(("SQLITE_THREADSAFE", "1"))
else:
    libraries.append("sqlite3")

pyttd_native = Extension(
    "pyttd_native",
    sources=sources,
    include_dirs=["ext"],
    libraries=libraries,
    define_macros=define_macros,
    extra_compile_args=extra_compile_args,
)

setup(ext_modules=[pyttd_native])
