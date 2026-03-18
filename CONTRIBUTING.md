# Contributing to pyttd

Thank you for your interest in contributing to pyttd! This project spans C, Python, and TypeScript — there are opportunities across the entire stack.

## Code of Conduct

By participating, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting Started

### Prerequisites

- Python 3.12+ with development headers
- C compiler (GCC or Clang)
- Node.js 18+ and npm (for VSCode extension work)
- Git

### Setup

```bash
git clone https://github.com/pyttd/pyttd.git
cd pyttd
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Or use the Makefile shortcut:

```bash
make dev
```

Verify:

```bash
.venv/bin/python -c "import pyttd_native; print('C extension OK')"
.venv/bin/pytest tests/ -v   # or: make test
```

For VSCode extension development:

```bash
cd vscode-pyttd
npm install
npm test   # or from root: make test-ts
```

Press **F5** in VSCode to launch the Extension Development Host with the extension loaded for testing.

See [docs/development/building.md](docs/development/building.md) for detailed build instructions including ASAN builds.

## Development Workflow

1. **Fork** the repository and create a feature branch
2. **Make your changes** — see areas for contribution below
3. **Write tests** for new functionality
4. **Run tests** before submitting:
   ```bash
   .venv/bin/pytest tests/ -v
   cd vscode-pyttd && npm test
   ```
5. **Rebuild the C extension** if you changed any files in `ext/`:
   ```bash
   .venv/bin/pip install -e .
   ```
6. **Submit a pull request** against `main`

## Areas for Contribution

### C Extension (`ext/`)

The recording engine, ring buffer, checkpointing, and I/O hooks. This is the most performance-sensitive part of pyttd.

- Adding new I/O hooks for deterministic replay
- Performance optimizations in the ring buffer or flush path
- Platform support improvements (Windows, new Python versions)

See [docs/development/c-extension.md](docs/development/c-extension.md) for architecture and invariants.

### Python Backend (`pyttd/`)

The CLI, JSON-RPC server, session navigation, query API, and ORM models.

- New navigation modes or query capabilities
- Server reliability improvements
- CLI enhancements

See [docs/api-reference.md](docs/api-reference.md) for the public API.

### VSCode Extension (`vscode-pyttd/`)

The debug adapter, timeline webview, CodeLens, inline values, and call history tree.

- UI improvements to the timeline scrubber
- New debug adapter features
- Accessibility improvements

### Tests

All layers benefit from more test coverage:

- Python tests in `tests/` (pytest)
- VSCode extension tests in `vscode-pyttd/src/test/` (Mocha)

See [docs/development/testing.md](docs/development/testing.md) for test conventions.

### Documentation

- Improve existing guides
- Add examples and tutorials
- Fix broken links or outdated information

## Code Style

### C

- Functions are NOT `static` — defined in `.c` files, declared in `.h` headers
- Use `#if PY_VERSION_HEX` for version-gated APIs
- Use `#ifdef PYTTD_HAS_FORK` for platform-conditional code
- Prefer C11 `<stdatomic.h>` for atomics
- NULL-check all `PyObject*` returns

### Python

- Follow existing code conventions in the file you're editing
- Use type hints where the surrounding code does
- Use `tmp_path` fixture for test DB isolation

### TypeScript

- Follow existing patterns in `vscode-pyttd/src/`
- Use the mock infrastructure in `src/test/mock-vscode.ts` for tests

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR
- Include tests for new functionality
- Update CHANGELOG.md for user-facing changes
- If you changed C code, mention that `pip install -e .` is needed to rebuild
- The PR template has a checklist — please review it

## Reporting Issues

Use the [issue templates](https://github.com/pyttd/pyttd/issues/new/choose) on GitHub:

- **Bug Report** — for bugs with reproduction steps
- **Feature Request** — for new feature proposals

## Questions?

Open a [Discussion](https://github.com/pyttd/pyttd/discussions) on GitHub for questions, ideas, or general conversation.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
