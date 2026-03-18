# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.3.x   | Yes                |
| < 0.3   | No                 |

## Reporting a Vulnerability

If you discover a security vulnerability in pyttd, please report it responsibly:

1. **Do not** open a public GitHub issue
2. [Report privately via GitHub Security Advisories](https://github.com/pyttd/pyttd/security/advisories/new)
3. Include steps to reproduce if possible
4. Allow reasonable time for a fix before public disclosure

We aim to acknowledge reports within 48 hours and provide a fix or mitigation within 30 days.

## Security Scope

pyttd is a **development tool** — it is not designed for production use. That said, we take the following security properties seriously:

### In Scope

- **Protocol limits** — The JSON-RPC server enforces a 1 MB header accumulation limit and a 10 MB Content-Length limit to prevent memory exhaustion
- **Non-ASCII header rejection** — Malformed headers are rejected
- **Local-only TCP** — The debug server binds to `127.0.0.1` (localhost only)
- **Fork safety** — Checkpoint children ignore signals (SIGINT, SIGTERM, SIGPIPE) and are properly reaped
- **No arbitrary code execution** — The server does not evaluate arbitrary Python expressions from the network; expression evaluation operates on recorded snapshots only

### Out of Scope

- **Recording overhead** — pyttd captures `repr()` snapshots of all local variables, which may include sensitive data. The `.pyttd.db` file should be treated as sensitive
- **Network exposure** — If you expose the TCP port beyond localhost, the server has no authentication. Do not do this
- **Denial of service** — The server is single-connection; resource limits are best-effort

## Known Security Properties

- The C extension uses `fork()` for checkpointing. Forked children inherit the full process state, including any secrets in memory
- Database files (`.pyttd.db`) contain full variable snapshots in `repr()` form. These may include credentials, tokens, or PII present in the recorded program's variables
- The `PYTTD_RECORDING=1` environment variable is set during recording — user scripts can check this to avoid recording sensitive operations
