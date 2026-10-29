# Changelog

## Unreleased

### Changed

- README and the `search_emails` description say that Yandex's text search index lags behind delivery: a message that arrived minutes ago is found by date and flags but may not match `query`, `sender`, `to` or `subject` yet.

## 0.6.0

### Deprecated

- `YANDEX_APP_PASSWORD` and `MCP_AUTH_TOKEN` stay for one more release and will be removed in 0.7.0, not 0.6.0 as announced earlier.

### Fixed

- Write access on the shared HTTP server. The protected-resource metadata advertised only `mail.read`, so every client asked for read access, and a client registered while `YANDEX_ALLOW_WRITE` was off kept a read-only registration for good; the server then asked Yandex for `mail:imap_ro` and issued read-only tokens, and reconnecting changed nothing. The metadata now lists `mail.write` when writes are enabled, and what a client may ask for follows the server's current configuration.

### Added

- A consent page before the Yandex sign-in: when the server allows writes and the client asks for them, the user chooses *read only* or *read and change mail*. Only the second choice requests full IMAP access from Yandex, and the issued token carries `mail.write` only after that choice and Yandex's grant. Refreshing a token never widens it.
- Browser confirmation for clients without elicitation on the shared HTTP server: a changing tool returns a single-use `confirm_url` where the signed-in owner approves or rejects the change within 10 minutes. The page requires the browser session that completed the Yandex sign-in, or a fresh Yandex sign-in for identity only.
- `get_connection_capabilities`: a read-only tool that reports whether writes are enabled, the connection's MCP scopes, whether the Yandex authorization has full IMAP access, how changes are confirmed, and what to do when they are unavailable, without revealing secrets.
- A hint when a nested folder is written with `/`: Yandex separates levels with `|`, and the error names the folder that matches.

## 0.5.1

### Fixed

- `move_email` and `archive_email` matched the destination folder without regard to case, so with folders that differ only by case the message could go to the wrong one. The name is now matched exactly first, then case-insensitively when unambiguous; an ambiguous name is rejected.
- A client without elicitation support got a generic "method not found" error from a changing tool. The server now checks the capability first and explains what is missing and, for reversible changes, how to skip the confirmation.
- `YANDEX_IMAP_PORT` and `MCP_PORT` outside 1–65535 are rejected at startup instead of failing later with a misleading DNS error.
- When a Yandex OAuth token stops working, tools report why and how to sign in again (`yandex-mail-mcp login`, a fresh `YANDEX_OAUTH_TOKEN`, or reconnecting the server in the MCP client) instead of the generic "IMAP authentication failed" message meant for passwords.

### Changed

- README documents every environment variable, where `.env` is found, the result fields and preview limits, what counts as an attachment, and the checks CI runs. SECURITY.md adds the supported versions and the security model.
- The multi-user HTTP server (`MCP_PUBLIC_URL`) no longer looks for the tokens saved by `yandex-mail-mcp login` at startup, so it does not touch the system keyring in a container.
- When several pooled IMAP connections are rejected at the same time, the stored Yandex token is refreshed once; the other connections reuse the refreshed token.
- The sign-in rate limit forgets idle client addresses once per minute instead of only after 10 000 addresses.

## 0.5.0

### Added

- Sign-in to IMAP with Yandex OAuth (`XOAUTH2`). `yandex-mail-mcp login` signs in through the browser and keeps the tokens in the system keyring; `yandex-mail-mcp logout` revokes them. `YANDEX_OAUTH_TOKEN` accepts a ready access token.
- A shared HTTP server for several users (`MCP_PUBLIC_URL`): an OAuth authorization server for MCP clients that delegates sign-in to Yandex and stores no user tokens, with `MCP_TOKEN_ENCRYPTION_KEYS`, `MCP_ALLOWED_USERS`, `MCP_AUTH_DB` and `MCP_AUTH_RATE_LIMIT`.
- The `logout` tool, per-user write permission from the granted Yandex scope, and a JSON audit log of tool calls.

### Deprecated

- `YANDEX_APP_PASSWORD`: works only with the stdio transport and logs a warning. It will be removed in 0.7.0 (originally announced for 0.6.0); use `yandex-mail-mcp login`.
- `MCP_AUTH_TOKEN` and its single shared bearer token: will be removed in 0.7.0 (originally announced for 0.6.0); use `MCP_PUBLIC_URL`.

### Removed

- `MCP_ALLOW_UNAUTHENTICATED_REMOTE`: a non-loopback HTTP server now requires `MCP_PUBLIC_URL` or `MCP_AUTH_TOKEN`.

### Changed

- `compose.yaml` runs the shared OAuth server and requires its variables; the `auth` volume keeps the registered MCP clients.
- `YANDEX_APP_PASSWORD` is refused by the HTTP server.
