import sys
import platform
from setuptools import setup, Extension

if sys.version_info < (3, 12):
    raise SystemExit("pyttd requires Python 3.12 or later")

extra_compile_args = []
if platform.system() == "Windows":
    extra_compile_args.append("/std:c11")

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
    ],
    include_dirs=["ext"],
    extra_compile_args=extra_compile_args,
)

setup(ext_modules=[pyttd_native])
