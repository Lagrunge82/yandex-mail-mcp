# Changelog

## Unreleased

### Fixed

- `move_email` and `archive_email` matched the destination folder without regard to case, so with folders that differ only by case the message could go to the wrong one. The name is now matched exactly first, then case-insensitively when unambiguous; an ambiguous name is rejected.
- A client without elicitation support got a generic "method not found" error from a changing tool. The server now checks the capability first and explains what is missing and, for reversible changes, how to skip the confirmation.
- `YANDEX_IMAP_PORT` and `MCP_PORT` outside 1–65535 are rejected at startup instead of failing later with a misleading DNS error.

### Changed

- README documents every environment variable, where `.env` is found, the result fields and preview limits, what counts as an attachment, and the checks CI runs. SECURITY.md adds the supported versions and the security model.

## 0.5.0

### Added

- Sign-in to IMAP with Yandex OAuth (`XOAUTH2`). `yandex-mail-mcp login` signs in through the browser and keeps the tokens in the system keyring; `yandex-mail-mcp logout` revokes them. `YANDEX_OAUTH_TOKEN` accepts a ready access token.
- A shared HTTP server for several users (`MCP_PUBLIC_URL`): an OAuth authorization server for MCP clients that delegates sign-in to Yandex and stores no user tokens, with `MCP_TOKEN_ENCRYPTION_KEYS`, `MCP_ALLOWED_USERS`, `MCP_AUTH_DB` and `MCP_AUTH_RATE_LIMIT`.
- The `logout` tool, per-user write permission from the granted Yandex scope, and a JSON audit log of tool calls.

### Deprecated

- `YANDEX_APP_PASSWORD`: works only with the stdio transport and logs a warning. It will be removed in 0.6.0; use `yandex-mail-mcp login`.
- `MCP_AUTH_TOKEN` and its single shared bearer token: will be removed in 0.6.0; use `MCP_PUBLIC_URL`.

### Removed

- `MCP_ALLOW_UNAUTHENTICATED_REMOTE`: a non-loopback HTTP server now requires `MCP_PUBLIC_URL` or `MCP_AUTH_TOKEN`.

### Changed

- `compose.yaml` runs the shared OAuth server and requires its variables; the `auth` volume keeps the registered MCP clients.
- `YANDEX_APP_PASSWORD` is refused by the HTTP server.
