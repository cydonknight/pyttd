import sys
import platform
from setuptools import setup, Extension

if sys.version_info < (3, 12):
    raise SystemExit("pyttd requires Python 3.12 or later")

extra_compile_args = []
if platform.system() == "Windows":
    extra_compile_args.append("/std:c11")
    extra_compile_args.append("/experimental:c11atomics")

libraries = []
if platform.system() != "Windows":
    libraries.append("sqlite3")

pyttd_native = Extension(
    "pyttd_native",
    sources=[
        "ext/pyttd_native.c",
        "ext/recorder.c",
        "ext/ringbuf.c",
        "ext/checkpoint.c",
        "ext/checkpoint_store.c",
        "ext/replay.c",
        "ext/iohook.c",
        "ext/sqliteflush.c",
    ],
    include_dirs=["ext"],
    libraries=libraries,
    extra_compile_args=extra_compile_args,
)

setup(ext_modules=[pyttd_native])
