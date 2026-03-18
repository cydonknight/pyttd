# Building pyttd

## Prerequisites

- **Python 3.12+** (3.12 or 3.13 recommended)
- **C compiler** — GCC or Clang (for the `pyttd_native` C extension)
- **Node.js 18+** and **npm** (for the VSCode extension, optional)
- **Git**

### macOS

```bash
# Xcode command line tools provide clang
xcode-select --install

# Node.js (optional, for VSCode extension)
brew install node
```

### Linux (Debian/Ubuntu)

```bash
sudo apt install python3-dev build-essential

# Node.js (optional, for VSCode extension)
sudo apt install nodejs npm
```

## Python Package

### Standard Build

```bash
# Create virtual environment
python3 -m venv .venv

# Install in editable mode with dev dependencies
# This compiles the C extension and installs pytest, pytest-benchmark, rich
.venv/bin/pip install -e ".[dev]"
```

The C extension is compiled automatically by `pip install`. The `setup.py` file defines `ext_modules` for the `pyttd_native` module, while `pyproject.toml` handles project metadata and dependencies.

### Rebuilding After C Changes

Any time you modify files in `ext/`, you must rebuild:

```bash
.venv/bin/pip install -e .
```

### ASAN Build (Address Sanitizer)

For detecting memory errors in the C extension:

```bash
CFLAGS="-fsanitize=address -fno-omit-frame-pointer" \
LDFLAGS="-fsanitize=address" \
.venv/bin/pip install -e .
```

Run tests with ASAN:

```bash
ASAN_OPTIONS=detect_leaks=0 .venv/bin/pytest tests/ -v
```

Note: `detect_leaks=0` is often needed because Python itself has known "leaks" that ASAN reports.

### Build System Notes

Both `pyproject.toml` and `setup.py` are required:
- `pyproject.toml` — project metadata, dependencies, build system configuration
- `setup.py` — C extension `ext_modules` definition (setuptools' pyproject.toml-only C extension support is limited)

The C extension compiles these source files:
- `ext/pyttd_native.c` — module init
- `ext/recorder.c` — PEP 523 eval hook, trace function, flush thread
- `ext/ringbuf.c` — lock-free SPSC ring buffer
- `ext/checkpoint.c` — fork-based checkpointing (Unix only)
- `ext/checkpoint_store.c` — checkpoint index and eviction
- `ext/replay.c` — checkpoint restore
- `ext/iohook.c` — I/O hooks for deterministic replay

## VSCode Extension

### Development Build

```bash
cd vscode-pyttd
npm install
npm run compile    # TypeScript → JavaScript
```

Press **F5** in VSCode to launch the Extension Development Host for testing.

### VSIX Packaging

```bash
cd vscode-pyttd
npm run package    # Produces pyttd-<version>.vsix
```

This uses esbuild to bundle the extension into a single file, then `@vscode/vsce` to package it as a `.vsix`.

### Extension Build Notes

- The extension uses esbuild for bundling (`esbuild.mjs`)
- Source is TypeScript in `src/`
- Tests use Mocha (`npm test` = `tsc -p ./ && mocha`)
- Dev dependencies: `mocha`, `@types/mocha`, `sinon`, `@types/sinon`

## Verifying the Build

```bash
# Check C extension imports
.venv/bin/python -c "import pyttd_native; print('OK')"

# Check CLI
.venv/bin/python -m pyttd --version

# Run Python tests
.venv/bin/pytest tests/ -v

# Run VSCode extension tests
cd vscode-pyttd && npm test
```

## See Also

- [Testing Guide](testing.md) — running and writing tests
- [C Extension Guide](c-extension.md) — C extension architecture and invariants
- [Contributing](../../CONTRIBUTING.md) — development workflow
