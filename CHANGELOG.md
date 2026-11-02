# Changelog

## 0.8.1

### Fixed

- Read-only tools failed with "IMAP connection was interrupted; please retry" when Yandex dropped an IMAP session during the call, although the same call worked at once when repeated. `list_folders`, `list_recent_emails`, `search_emails`, `get_email` and `get_thread` now run the whole call once more, `SELECT` included, on a new connection when the session drops (`BYE`, end of stream, a reset or broken socket, a TLS EOF). A second drop in a row fails with "IMAP connection was interrupted twice; the mail server may be closing sessions, retry later". Changes are never repeated, since the session may drop after the server made the change, and a timeout is not retried either.
- After a dropped session the pool closes its idle connections too, instead of handing them to the next calls, and the retry closes any idle ones untried before it opens a new connection, so the pool never holds more than `YANDEX_IMAP_POOL_SIZE` sessions. Dropped and discarded connections are closed without `LOGOUT`, which on a session dropped without a reset would wait for the socket timeout. A failure to connect or log in is not a dropped session and is not retried.
- A dropped session is now logged: a warning names the operation, the error with the server's reason, whether the connection was new or reused, its age, the number of calls it served and the IMAP state. A connection replaced after a failed `NOOP` is logged at INFO. Neither carries message content or tokens; a server line the client did not expect, or one cut short, is logged without its text.

### Added

- `scripts/imap_stress.py` runs read-only calls against the configured mailbox, one after another and then all at once, and reports failures, response times, dropped sessions and retries.

## 0.8.0

### Security

- Refresh tokens of the shared HTTP server rotate with reuse detection. Each refresh token carries the generation it was issued in, and `MCP_AUTH_DB` keeps the generation each sign-in is at. Exchanging a refresh token claims the next generation atomically, before Yandex is asked. A refresh token works once, and one used again, by the client or by someone with a copy, revokes its whole sign-in. Two concurrent refreshes with one token count as reuse. This holds across restarts and does not depend on Yandex, whose refresh token may be reusable. A refresh that fails before a new token is issued releases its claim, whatever stopped it, a cancelled request included, so the client can retry with the same token. Refresh tokens issued before this release count as generation 0 and work once more.
- The browser confirmation page is protected against cross-site request forgery. A decision is accepted only when it comes from the server's own origin, by `Origin`, or `Referer` when a browser drops `Origin`, and carries a form token. The token is sealed for the change, the user and the browser that opened the page, and expires with the change. A new sign-in of the same user in that browser keeps its id, so an open page still works. A malformed `Origin` is refused, and a default port written out in `MCP_PUBLIC_URL` matches a browser's `Origin` without it. A page on a sibling subdomain, which receives the SameSite=Lax cookie, can no longer approve or reject a change by knowing its `confirm_url`. A refused decision leaves the change waiting. Every HTML page of the server is sent with `Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`.
- Revoking access on the shared HTTP server ends that sign-in's tokens on the server's own record. Each sign-in now has a random authorization id that its tokens carry, refreshed ones included. The `logout` tool and `/revoke` record the id in `MCP_AUTH_DB` for 30 days, and every request and refresh checks it, so a revoked token stops working at once whatever Yandex does, and signing in again never revives it. Changes of the revoked sign-in waiting for browser confirmation are dropped, and an approval that arrives after the revocation changes nothing.
- IMAP connections on the shared server are pooled per sign-in instead of per Yandex login. A connection logged in with one sign-in's token never serves another, so the user's other connections can no longer keep a revoked one working. A revoked sign-in's pool is closed at once, along with the same user's other pools, which log in again with their own tokens. Every 30 seconds the server closes the pools unused for `YANDEX_IMAP_IDLE_TIMEOUT` seconds, but at least a minute, so abandoned sign-ins do not hold IMAP sessions.

### Fixed

- A confirmed change could act on another message. The confirmation and the change were tied only by folder name and UID, and after a folder is deleted and created again the same UID can name another message. The confirmation now records the source folder's `UIDVALIDITY` when it reads the messages, and the change compares it right after `SELECT`. If it differs, or is no longer reported, nothing moves or is flagged, `move_to_promotions` creates no folder, and the error asks for a new confirmation. This applies to `move_email`, `move_emails`, `archive_email`, `mark_read`, `set_flag` and `move_to_promotions`, confirmed through elicitation or in the browser.
- A change approved on the shared HTTP server could be refused as read-only. Requests of one authorization replaced the credentials of its shared mailbox, so a request with another token arriving while the user decided changed what the approved write was checked against. Each request now works on its own view of the mailbox: it shares the connection pool but keeps the request's credentials. The write check, the logins of new connections, and a change waiting for elicitation or browser approval all use the credentials of the request that asked. A pool is shared only by tokens of one sign-in with the same Yandex scope, so a connection logged in read-only never serves a full-access request. A read-only token still cannot write.

### Changed

- Tokens issued by earlier versions carry no authorization id and are refused: every MCP client of the shared server signs in again once after the upgrade.
- `logout` ends the calling connection's sign-in even when Yandex cannot be reached, and its result has a new `yandex_revoked` field. When Yandex did not revoke its token, the result says so instead of failing, since the connection could not call `logout` again.
- Losing `MCP_AUTH_DB` now also forgets revocations; README suggests replacing `MCP_TOKEN_ENCRYPTION_KEYS` with a new key after such a loss.

## 0.7.1

### Fixed

- A client that declares elicitation over a transport that cannot deliver it (the stateless HTTP server, or the single-exchange protocol, where the SDK raises `NoBackChannelError`) is now confirmed in the browser on the shared server, like a client without elicitation: the changing tools return `confirmation_required` with a `confirm_url` instead of failing, and nothing changes until the owner approves. The server checks the channel before asking and also catches the error if the send fails anyway; a declined confirmation or any other error never falls back. On the local server the error names the transport. `get_connection_capabilities` reports `confirmation: browser_link` for such connections and a new `transport_delivers_elicitation` field.

## 0.7.0

### Removed

- `YANDEX_APP_PASSWORD` and `MCP_AUTH_TOKEN`, deprecated in 0.5.0. The server signs in to IMAP only with Yandex OAuth (`yandex-mail-mcp login` or `YANDEX_OAUTH_TOKEN`), and an HTTP server reachable from other machines needs `MCP_PUBLIC_URL`. A server started with either removed variable stops with a message naming the replacement.

### Added

- `move_emails`: move up to 25 messages from one folder to another with a single confirmation that lists every message's sender and subject. One IMAP `MOVE` carries the batch, and the result reports which UIDs moved and which did not from the server's `COPYUID` (without UIDPLUS, which are still in the source folder), since a server may move part of a set; the same folder rules, write checks and audit apply as for `move_email`.

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
