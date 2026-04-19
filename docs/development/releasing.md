# Releasing

Guide for maintainers on how to release new versions of pyttd.

## Version Locations

The version must be updated in **three places**:

1. `pyttd/__init__.py` — `__version__ = "X.Y.Z"`
2. `pyproject.toml` — `version = "X.Y.Z"`
3. `vscode-pyttd/package.json` — `"version": "X.Y.Z"`

## Release Process

### 1. Update Version

```bash
# Update all three version locations
# pyttd/__init__.py
# pyproject.toml
# vscode-pyttd/package.json
```

### 2. Update CHANGELOG.md

Move items from `[Unreleased]` to a new version section:

```markdown
## [X.Y.Z] - YYYY-MM-DD

### Added
- ...

### Fixed
- ...
```

### 3. Verify

```bash
# Run all Python tests
.venv/bin/pytest tests/ -v

# Run VSCode extension tests
cd vscode-pyttd && npm test && cd ..

# Verify version
.venv/bin/python -m pyttd --version

# Build sdist and wheel
.venv/bin/python -m build
```

### 4. Commit and Tag

```bash
git add -A
git commit -m "Release vX.Y.Z"
git tag -a vX.Y.Z -m "vX.Y.Z"
```

### 5. PyPI Release

```bash
# Build
python -m build

# Upload to PyPI
python -m twine upload dist/*
```

The GitHub Actions CI also builds wheels for Linux and macOS via cibuildwheel.

### 6. VSIX Release

```bash
cd vscode-pyttd
npm run package
# Produces pyttd-X.Y.Z.vsix
```

Upload the `.vsix` to the VSCode marketplace via `vsce publish` or manual upload.

### 7. GitHub Release

Create a GitHub release from the tag:

```bash
gh release create vX.Y.Z --title "vX.Y.Z" --notes "See CHANGELOG.md for details."
```

Attach the VSIX file to the release.

### 8. Post-Release

- Bump version to the next dev version (e.g., `0.9.0` after releasing `0.8.0`) in all three locations
- Add new `[Unreleased]` section to CHANGELOG.md

## Versioning Policy

pyttd follows [Semantic Versioning](https://semver.org/):

- **Major** (1.0.0) — breaking API changes
- **Minor** (0.X.0) — new features, backward compatible
- **Patch** (0.0.X) — bug fixes only

While below 1.0, minor version bumps may include breaking changes.

## See Also

- [Building](building.md) — build instructions
- [Testing](testing.md) — running tests before release
- [Contributing](../../CONTRIBUTING.md) — development workflow
