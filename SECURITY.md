# Security Policy

## Supported versions
Pre-release (v0.x): only the latest commit on `main`.

## Reporting
Report security issues privately to the maintainer (GitHub security advisory
once public). Do not open public issues for exploitable details.

## Scope notes
- CostGuard is offline-first; it does not transmit user data to servers.
- Sensitive-class bugs include: any code path that writes to user original
  files, secret leakage into logs/backups/exports, path traversal in
  workspace handling.
