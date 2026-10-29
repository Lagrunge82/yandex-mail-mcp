# Yandex Mail MCP

A small, self-hosted **MCP server for Yandex Mail**. Built with Python and IMAP. It reads mail without marking it read, and changes mail only behind an explicit opt-in and a confirmation from the user.

**Project status:** early development (0.x). Tagged releases and container images are published, but tool names, parameters and configuration may still change between minor versions. See [CHANGELOG.md](CHANGELOG.md).

- [What it does](#what-it-does)
- [Quick start for one user (stdio)](#quick-start-for-one-user-stdio)
- [Shared server for several users (HTTP)](#shared-server-for-several-users-http)
- [Write access and confirmations](#write-access-and-confirmations)
- [Configuration](#configuration)
- [Security](#security)
- [Docker image](#docker-image)
- [Safety and permissions](#safety-and-permissions)
- [Development](#development)

## What it does

Read tools, always available:

- `list_folders`: list the mailbox's IMAP folders, named as the server reports them (Yandex separates nested folders with `|`).
- `list_recent_emails`: inspect the most recent messages of a folder, newest first, optionally unread only.
- `search_emails`: search a folder by any combination of `query` (IMAP `SEARCH TEXT`, which matches headers and body as the server indexes them), `sender`, `to`, `subject`, `since` and `before` (`YYYY-MM-DD`; IMAP compares dates without time, in the server's time zone), `unread`, `flagged` and `has_attachments`. The server evaluates every criterion except `has_attachments`, which is checked against the MIME structure of at most the 500 newest messages matching the rest. If the server rejects a search by `query` alone, it falls back to the 300 newest messages and matches only the sender, subject and the first 2000 characters of the text preview, so results can differ from a server search; searches with other criteria report the server error instead. Yandex evaluates `query`, `sender`, `to` and `subject` against a search index that lags behind delivery: a message that arrived minutes ago is listed by `list_recent_emails` and found by `since`, `unread` or `flagged`, but may not match text criteria yet. For the newest mail, list the folder or search by date.
- `get_email`: inspect one message by IMAP UID.
- `get_thread`: collect the conversation a message belongs to, oldest first, by following `Message-ID`, `In-Reply-To` and `References` through the message's folder, INBOX and the folder flagged `\Sent`. Each result names its folder. Yandex can fail a search by `Message-ID` with `[UNAVAILABLE] UID SEARCH Backend error`; the earlier messages of the thread are then found by subject among at most 200 messages per folder, and compared by `Message-ID`.

Changing tools, which need `YANDEX_ALLOW_WRITE=true`, act on one message per call (or on one reviewed batch with `move_emails`) and ask the user to confirm (see [Safety and permissions](#safety-and-permissions)):

- `move_email`: move one message to an existing folder.
- `move_emails`: move up to 25 messages from one folder to another in one step. The confirmation lists every message with its sender and subject. One IMAP `MOVE` carries the batch; since a server may move part of it, the result reports `moved` and `not_moved` from the server's `COPYUID` response, or, on a server without UIDPLUS, which UIDs are still in the source folder and a warning to check the destination.
- `archive_email`: move one message to the archive folder: `YANDEX_ARCHIVE_FOLDER`, or the folder the server flags `\Archive`.
- `mark_read`: mark one message read or unread.
- `set_flag`: flag or unflag one message.
- `create_draft`: save a plain-text draft, optionally a reply that joins a message's conversation, in the folder flagged `\Drafts`. Nothing is sent: the user reviews and sends it from Yandex Mail.
- `move_to_promotions`: deprecated in favor of `move_email` and will be removed in a later release. Moves one inbox message to the dedicated folder `YANDEX_PROMOTIONS_FOLDER`, creating it if missing.

`get_connection_capabilities` reports what the current connection may do: whether the server allows changes, which MCP scopes the connection holds, whether the Yandex authorization behind it has full IMAP access, whether the client can confirm changes itself or through a link, and what to do when changes are unavailable. It reveals no tokens or secrets.

On the [shared HTTP server](#shared-server-for-several-users-http), `logout` signs the caller out: it revokes their Yandex access, which ends every token the server issued for it. Mail is not changed. The local server has the `logout` command instead.

The server exposes no deletion, expunge, move to Trash, or sending of mail.

### Results

Every message result has `uid`, `message_id`, `subject`, `from`, `to`, `date` (ISO 8601 when the header parses), `preview`, `has_attachments`, `attachments`, `list_unsubscribe` (the raw header; the server never follows unsubscribe links or sends unsubscribe mail), `in_reply_to` and `references`. `get_thread` results add `folder`.

- **UIDs are unique only within a folder.** Pass the same `folder` to `get_email`, `get_thread` and the changing tools that you listed or searched with. A message moved to another folder gets a new UID there.
- **Paging:** `list_recent_emails` and `search_emails` return at most 100 messages per call. For the next page, pass the smallest `uid` of a page as `before_uid`.
- **Previews** are the message's first `text/plain` part, or its first `text/html` part converted to text, collapsed to single spaces. Lists and searches fetch headers, the MIME structure and at most 16 KiB of that part per message, without fetching attachment sections; the preview is cut at 4000 characters in `list_recent_emails` and 2000 in `search_emails` and `get_thread`. `get_email` downloads the complete message, including attachments, subject to a 20 MiB size limit, and returns up to 10000 characters.
- **Attachments:** contents are never returned; `attachments` lists each part's file name, content type, size and IMAP section. Any part that is not text or multipart, has a file name, or has an `attachment` disposition counts, so inline images in HTML signatures and attached messages count as attachments. The size is the encoded size stored in the message, about a third larger than the file for base64 content.
- **Read operations never mark mail as read:** they use IMAP `BODY.PEEK` and select folders read-only.

## Quick start for one user (stdio)

The server signs in to IMAP with a Yandex OAuth token (`AUTHENTICATE XOAUTH2`); no password is needed. Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/).

1. In Yandex Mail settings → **Email clients**, enable IMAP access and **App passwords and OAuth tokens**. For a Yandex 360 account, the administrator must also allow OAuth access to mail.
2. Register an application at [oauth.yandex.ru](https://oauth.yandex.ru/) of the type **for user authorization** («Для авторизации пользователей»; [Yandex instructions](https://yandex.ru/dev/id/doc/ru/register-auth)). The type for API access or debugging cannot use a loopback redirect. Choose the **Web services** platform with the redirect URI `http://127.0.0.1:8765/callback` (the port is `YANDEX_OAUTH_LOOPBACK_PORT`), and the permissions `login:email`, `login:info`, `mail:imap_ro` (reading mail) and `mail:imap_full` (shown by Yandex as reading and deleting mail; the server never deletes). Signing in works with PKCE alone, without the client secret; still set `YANDEX_OAUTH_CLIENT_SECRET`, since token refresh has only been checked with it.
3. Install, configure and sign in:

   ```bash
   uv sync --locked
   cp .env.example .env
   # Set YANDEX_OAUTH_CLIENT_ID (and YANDEX_OAUTH_CLIENT_SECRET if Yandex issued one).
   uv run yandex-mail-mcp login
   ```

   A browser opens the Yandex sign-in page. After you allow access, the tokens are saved in the system keyring, or in `~/.config/yandex-mail-mcp/tokens.json` (mode 0600, with a warning) where no keyring exists. The server refreshes the access token itself, which needs the same `YANDEX_OAUTH_CLIENT_ID` (and secret, if one was used at login) in its environment.
4. Add the server to your MCP client as a stdio command, for example in Claude Desktop's `claude_desktop_config.json`:

   ```json
   {"mcpServers": {"yandex-mail": {"command": "uv", "args": ["--directory", "/path/to/yandex-mail-mcp", "run", "yandex-mail-mcp"]}}}
   ```

   or with Claude Code: `claude mcp add yandex-mail -- uv --directory /path/to/yandex-mail-mcp run yandex-mail-mcp`. Variables can also be passed in the client's `env` block instead of a `.env` file. The server logs to standard error, so logging never corrupts the stdio transport.

`uv run yandex-mail-mcp logout` revokes the token at Yandex and deletes it. Without `YANDEX_ALLOW_WRITE=true`, `login` asks only for read-only IMAP access; with it, for full access. A read-only token makes the changing tools fail with a hint to sign in again.

Credentials come from the first of these that is set: `YANDEX_OAUTH_TOKEN` with `YANDEX_EMAIL` (a ready access token, for CI and containers; it cannot be refreshed), then the tokens saved by `login`. The app password of earlier releases is no longer accepted: a set `YANDEX_APP_PASSWORD` stops the server with a message pointing at `login`.

For a single-user HTTP endpoint on this machine, set `MCP_TRANSPORT=streamable-http`: the endpoint is `http://127.0.0.1:8000/mcp`, reachable without a token from the loopback interface only. A non-loopback `MCP_HOST` requires `MCP_PUBLIC_URL` (below); the static `MCP_AUTH_TOKEN` of earlier releases is no longer accepted.

## Shared server for several users (HTTP)

With `MCP_PUBLIC_URL` set, the HTTP server is an OAuth authorization server for MCP clients: each person adds the server's URL to their client, signs in with Yandex in the browser and works with their own mailbox. The server stores no user tokens or passwords (see [Security](#security)).

1. Register a Yandex OAuth application as above, with the redirect URI `<MCP_PUBLIC_URL>/oauth/yandex/callback`, for example `https://mail.example.com/oauth/yandex/callback`. Note its client ID and client secret.
2. Generate an encryption key:

   ```bash
   python -c "import secrets, base64; print('k1:' + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```

3. In `.env`, set:

   | Variable | Value |
   |---|---|
   | `MCP_PUBLIC_URL` | The HTTPS URL clients use, such as `https://mail.example.com`. It is the issuer, the resource (`<url>/mcp`) and the base of the Yandex redirect URI. |
   | `MCP_TOKEN_ENCRYPTION_KEYS` | The generated key. |
   | `MCP_ALLOWED_USERS` | Comma-separated Yandex logins or addresses allowed to sign in. Empty refuses everyone. |
   | `YANDEX_OAUTH_CLIENT_ID`, `YANDEX_OAUTH_CLIENT_SECRET` | The application from step 1. |
   | `YANDEX_ALLOW_WRITE` | `true` to offer the changing tools; each user still has to grant full IMAP access. |

4. Start it with Docker Compose (`docker compose up -d --build`), which binds `127.0.0.1:8000` on the host and keeps the registered clients in the `auth` volume (`MCP_AUTH_DB`). Compose refuses to start without the variables above. Losing that volume only makes MCP clients register again; tokens already issued keep working.
5. Put an HTTPS reverse proxy in front of it. Without HTTPS, tokens travel in clear text. With [Caddy](https://caddyserver.com/), which obtains the certificate itself, a `Caddyfile` of two lines is enough:

   ```
   mail.example.com
   reverse_proxy 127.0.0.1:8000
   ```

6. Add the connector: in Claude Desktop, **Settings → Connectors → Add custom connector** with the URL `https://mail.example.com/mcp`; in Claude Code, `claude mcp add --transport http yandex-mail https://mail.example.com/mcp`, then `/mcp` to sign in. The client registers itself and opens the sign-in flow. When the server allows writes, a page on this server first asks whether the app may **read only** or **read and change mail**; Yandex then shows its own permission page. See [Write access and confirmations](#write-access-and-confirmations).

Requests to `/authorize`, `/token`, `/register`, `/revoke` and the Yandex sign-in routes are limited to `MCP_AUTH_RATE_LIMIT` (30) per client IP and minute. Compose sets `MCP_FORWARDED_ALLOW_IPS=*` so the limit sees the client addresses the proxy forwards; this is safe only because the port is bound to localhost. Each tool call writes a JSON line to the `yandex_mail_mcp.audit` log with the time, Yandex login, client ID, tool, folder, UID, result and duration, without subjects, addresses, message text or tokens.

`GET /healthz` answers `200 ok` while the HTTP server is serving, without logging in to IMAP or requiring a token; Compose uses it as the health check. DNS rebinding protection is enabled: the host of `MCP_PUBLIC_URL` and a specific `MCP_HOST` are allowed automatically, and `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` override the defaults. A wildcard bind such as `0.0.0.0` without `MCP_PUBLIC_URL` needs `MCP_ALLOWED_HOSTS` set to the host names or addresses and ports clients send.

## Write access and confirmations

Changing mail takes three separate permissions, and all three are checked on every call:

1. **The operator** enables changes with `YANDEX_ALLOW_WRITE=true`. Without it the server never asks Yandex for more than read access and never offers the `mail.write` scope.
2. **The user** grants them when connecting. On the shared HTTP server a consent page asks whether the app may *read only* or *read and change mail*; only the second choice makes the server request full IMAP access (`mail:imap_full`) from Yandex, and Yandex shows its own permission page after it. The MCP token gets the `mail.write` scope only when the user chose it and Yandex granted full access. A client that asks for `mail.read` alone goes straight to read-only access and sees no page. On the local server, `yandex-mail-mcp login` asks for full access when `YANDEX_ALLOW_WRITE=true` is set.
3. **The user confirms each change.** Every changing tool shows what it is about to do before doing it, with each message's sender and subject marked as untrusted text: one message per call, or, with `move_emails`, one list of up to 25 messages approved together. Clients with MCP elicitation (Claude Desktop, Claude Code) ask inside the conversation. On the shared HTTP server, a client without elicitation gets a link instead: the tool returns `confirmation_required` with a `confirm_url` on this server, where the user, signed in as the same Yandex account, sees the change and approves or rejects it. Nothing happens until they approve; the link is single-use and expires in 10 minutes. Changing the Seen and Flagged flags and saving a draft are reversible, so the operator can skip their confirmation with `MCP_CONFIRM_REVERSIBLE=false`; moves and archiving are always confirmed.

The confirmation page relies on a cookie set in the browser that completed the Yandex sign-in, so it is normally available right away. In another browser, or after 30 days, the page first sends the user through a Yandex sign-in that asks only for their login, then shows the change. The assistant cannot approve on the user's behalf: the page needs that browser session, and approving is a form submission from it.

### ChatGPT

ChatGPT connects to the shared HTTP server as any other MCP client, with the same consent page. Its OAuth client asks for the scopes this server publishes in its protected-resource metadata, which include `mail.write` when writes are enabled, so the consent page appears and the user chooses. Whether a given ChatGPT connection can confirm changes through elicitation is best checked with `get_connection_capabilities`: `confirmation` is `elicitation` when it can, and `browser_link` when changes will be confirmed through the link described above. Both work; the link is the fallback for clients that cannot ask.

### Upgrading a read-only connection

A connection authorized before writes were enabled, or with *read only* chosen on the consent page, keeps read-only tokens for as long as it refreshes them: refreshing never widens a token. To get write access, authorize again:

1. The operator sets `YANDEX_ALLOW_WRITE=true` and restarts the server.
2. In the client, disconnect and reconnect the server (ChatGPT: remove the connector and add it again, or use its reauthorize option when offered; Claude Desktop: remove and re-add the custom connector; Claude Code: `claude mcp remove yandex-mail`, add it again and run `/mcp`).
3. On the consent page choose **Read and change mail**, then allow full access on Yandex's page.

Clients registered while writes were off can do this without re-registering: what a client may ask for follows the server's current configuration. Tokens issued earlier stay read-only until they expire or are refreshed into nothing wider than they were.

### Troubleshooting

- **"Your access is read-only. Sign in again with write access: reconnect this server in your MCP client and allow full mail access"** means this connection's token lacks `mail.write`: either the connection was authorized before `YANDEX_ALLOW_WRITE=true`, or *read only* was chosen, or Yandex granted only `mail:imap_ro`. Reconnecting alone does not help if the client reuses its refresh token; follow [Upgrading a read-only connection](#upgrading-a-read-only-connection). `get_connection_capabilities` shows `mcp_scopes` and `yandex_full_mail_access` for the connection.
- **"Write operations are disabled. Set YANDEX_ALLOW_WRITE=true"**: the server itself refuses changes; this is the operator's setting.
- **"… needs the user's confirmation, but this MCP client does not support elicitation"** appears only on the local server: there is no browser identity to confirm with. Use a client with elicitation, or the shared HTTP server.
- **"Folder 'github/ci/cd' not found"**: nested folders are written with the server's delimiter, `|` on Yandex (`github|ci/cd`); the error names the folder it thinks you meant. `list_folders` shows the exact names.

## Configuration

Settings come from environment variables. The server and the `login` and `logout` commands also read a `.env` file, found by searching upward from the installed package (the project checkout when installed with `uv sync` or `pip install -e .`), and never override a variable that is already set. Booleans accept `1`, `true`, `yes` and `on`; a blank value means the default.

| Variable | Default | Meaning |
| --- | --- | --- |
| `YANDEX_OAUTH_CLIENT_ID` | empty | The Yandex OAuth application. Needed by `login`, by the stdio server to refresh the saved tokens, and by the shared HTTP server. |
| `YANDEX_OAUTH_CLIENT_SECRET` | empty | Its secret. `login` uses PKCE and signs in without it, but token refresh has only been checked with the secret set. |
| `YANDEX_OAUTH_LOOPBACK_PORT` | `8765` | Port of the `http://127.0.0.1:<port>/callback` redirect URI that `login` listens on. |
| `YANDEX_OAUTH_TOKEN` | empty | A ready Yandex access token, used with `YANDEX_EMAIL` instead of `login`. It cannot be refreshed. |
| `YANDEX_EMAIL` | empty | The mailbox address, required with `YANDEX_OAUTH_TOKEN`. |
| `YANDEX_IMAP_HOST` | `imap.yandex.com` | IMAP server. |
| `YANDEX_IMAP_PORT` | `993` | IMAP port, 1 to 65535; the connection always uses TLS. |
| `YANDEX_IMAP_POOL_SIZE` | `2` | Logged-in IMAP connections per user kept and reused across tool calls, at least 1. |
| `YANDEX_IMAP_IDLE_TIMEOUT` | `300` | Seconds an idle connection is kept before it is replaced. |
| `YANDEX_ALLOW_WRITE` | `false` | Enables the changing tools. `login` then asks for full IMAP access; HTTP users must grant it too. |
| `MCP_CONFIRM_REVERSIBLE` | `true` | Set to `false` to change the Seen and Flagged flags and save drafts without a confirmation request. Moves are always confirmed. |
| `YANDEX_ARCHIVE_FOLDER` | empty | Destination of `archive_email`; empty means the folder the server flags `\Archive`. Never created. |
| `YANDEX_PROMOTIONS_FOLDER` | `AI-Promotions` | Destination of the deprecated `move_to_promotions`. Must be a top-level, non-system folder. |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |
| `MCP_HOST` | `127.0.0.1` | HTTP bind address. |
| `MCP_PORT` | `8000` | HTTP port, 1 to 65535. |
| `MCP_PUBLIC_URL` | empty | The HTTPS URL of the shared HTTP server; setting it turns on OAuth sign-in for several users. |
| `MCP_TOKEN_ENCRYPTION_KEYS` | empty | `kid:base64key` pairs, newest first, that seal the tokens the shared server issues. Required with `MCP_PUBLIC_URL`. |
| `MCP_ALLOWED_USERS` | empty | Yandex logins or addresses allowed on the shared server, comma-separated. Empty refuses everyone. |
| `MCP_AUTH_DB` | `mcp-auth.db` | SQLite file of the MCP clients registered with the shared server. |
| `MCP_AUTH_RATE_LIMIT` | `30` | Requests per client IP and minute to the sign-in and token endpoints. |
| `MCP_FORWARDED_ALLOW_IPS` | empty | Proxies whose `X-Forwarded-For` the HTTP server trusts for client addresses. |
| `MCP_ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*,[::1]:*`, plus `MCP_HOST:*` for a specific address and the host of `MCP_PUBLIC_URL` | `Host` header values accepted by the DNS rebinding protection. |
| `MCP_ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*,http://[::1]:*`, plus the origin of `MCP_PUBLIC_URL` | `Origin` header values accepted from browsers. |

The server keeps up to `YANDEX_IMAP_POOL_SIZE` logged-in IMAP connections per user and reuses them across tool calls, so a series of calls logs in once. Yandex limits concurrent IMAP sessions per account, so keep the pool small. A call that finds every connection busy waits up to a minute, then fails with an error. A connection idle longer than `YANDEX_IMAP_IDLE_TIMEOUT` seconds or failing `NOOP` is replaced. Connections are returned with no folder selected, closed after a failed call, and logged out when the server stops. Yandex's IMAP server is `imap.yandex.com:993` (SSL); the server does not use SMTP or send mail.

## Security

- **Nothing about users is stored.** The server issues its own tokens, and each one carries the user's Yandex token inside, encrypted with AES-256-GCM under `MCP_TOKEN_ENCRYPTION_KEYS`. A Bearer token holds the Yandex access token, login, address and granted scope, the client ID and the expiry; a refresh token holds the Yandex refresh token instead. Access tokens last an hour (less if the Yandex token expires sooner), refresh tokens 30 days, authorization codes 60 seconds. The only file is the table of registered MCP clients, whose secrets are encrypted too.
- **What the server holds** is the encryption key and the Yandex application's `client_secret`. Neither is a user credential, but the key together with an intercepted token gives access to that user's mail: keep both in secret storage and serve only over HTTPS.
- **Revoking access**: the `logout` tool, or a client's call to `/revoke`, revokes the user's Yandex token, after which every token the server issued for it stops working. A user can also revoke the application in their Yandex ID settings. `MCP_ALLOWED_USERS` decides who can sign in at all and is checked on every token, so removing a user ends their access at once; a refused user's Yandex token is revoked immediately.
- **Rotating the key**: put the new key first (`MCP_TOKEN_ENCRYPTION_KEYS=k2:...,k1:...`); new tokens use it and old ones still open. After 30 days, when the last refresh token sealed with the old key has expired, remove it. Removing a key immediately ends every token sealed with it, and clients registered with a secret have to register again.
- **Write access** requires `YANDEX_ALLOW_WRITE=true` on the server, the user's choice of *read and change mail* on the consent page, and full IMAP access granted by the user at Yandex; the server asks Yandex for full access only after that choice, and refreshing a token never widens it. Each change is then confirmed by the user, in the client or on this server's confirmation page, which only the browser that signed in can use. The server still never deletes, expunges or sends mail.
- **The local server** keeps the tokens from `login` in the system keyring, or in a 0600 file where no keyring exists. Credentials are never logged or returned in errors, and the IMAP connection always uses TLS with certificate verification.

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Removed: app password and static token

Since 0.7.0 the server signs in to IMAP only with Yandex OAuth. `YANDEX_APP_PASSWORD` and the shared bearer token `MCP_AUTH_TOKEN` (deprecated in 0.5.0) are gone; a server started with either stops with a message naming the replacement: `yandex-mail-mcp login` or `YANDEX_OAUTH_TOKEN` for the local server, `MCP_PUBLIC_URL` for a server reachable from other machines. `MCP_ALLOW_UNAUTHENTICATED_REMOTE` was removed in 0.5.0.

## Docker image

CI publishes `ghcr.io/lagrunge82/yandex-mail-mcp` for Linux amd64 and arm64 after
the checks pass on `main` or a `v*` tag. When a push to `main` increases
`project.version` in `pyproject.toml`, CI creates the matching Git tag
(`v<version>`) and publishes the image with that tag. A conflicting existing
Git tag fails the workflow instead of being moved. The GitHub Actions token does
not trigger another workflow for the new tag, so the same run publishes the
versioned image. Versions must be valid Docker tags (PEP 440 local versions with
`+` are unsupported). A push without a previous commit to compare with (the
branch's first push, or some force pushes) creates no version tag. The `main`
image also has the `latest` and commit-SHA tags;
manually pushed `v*` tags produce matching image tags. Each publication
also gets a unique `build-<run_id>-<run_attempt>` tag. Registry tags can still be
overwritten; for a strictly immutable deployment, use the `image@sha256:...` digest
reference recorded in the workflow run summary. Package visibility
is managed in GitHub Packages; access to a private image requires registry login.
For example, after the first publication:

```bash
docker pull ghcr.io/lagrunge82/yandex-mail-mcp:latest
```

The image is based on Python 3.13, runs as an unprivileged user, owns `/data` for `MCP_AUTH_DB` and starts `yandex-mail-mcp` with the stdio transport by default. It contains no `.env` file and no browser for `login`, so pass a token instead, for example
`docker run -i --rm -e YANDEX_EMAIL -e YANDEX_OAUTH_TOKEN ghcr.io/lagrunge82/yandex-mail-mcp:latest`, or use the [shared HTTP server](#shared-server-for-several-users-http), which Compose builds from this image.

## Safety and permissions

Default configuration is read-only:

```dotenv
YANDEX_ALLOW_WRITE=false
```

The `folder` parameter on read tools can access any folder available to the account, including Sent and Drafts. Read-only mode restricts mutations; it does not restrict which folders can be read.

To change mail via MCP, set `YANDEX_ALLOW_WRITE=true` and authorize the connection with write access (see [Write access and confirmations](#write-access-and-confirmations)). Each changing tool acts on one message and asks for confirmation through MCP elicitation: the user must accept with `approve=true`, otherwise nothing changes. On the shared HTTP server, a client without elicitation gets a single-use link to a confirmation page on this server instead; on the local server such a client gets an error that says so. The request names the folders involved and quotes the message's sender and subject as untrusted single lines. Moves and archiving are always confirmed. Changing the Seen and Flagged flags and saving a draft are reversible, so the operator can skip their confirmation with `MCP_CONFIRM_REVERSIBLE=false`. Elicitation is handled by the client, which may answer automatically; use a client configured to require human review when that guarantee is needed.

Every change is validated before the user is asked and again after the approval: an invalid request fails before the confirmation, and a folder that disappears in between fails the change.

`move_email` and `archive_email` move only into existing folders and never create one. The destination is matched exactly as `list_folders` spells it, or case-insensitively when only one folder matches; a name that matches several folders differing only by case is rejected. They refuse Trash, Spam, Drafts and Sent (by their special-use flags or system names, including the Russian names Yandex uses), folders inside them, and virtual folders such as All Mail. `create_draft` appends a plain-text message (body up to 100 KiB, no attachments or HTML) to the drafts folder with the `\Draft` flag; its `From` is the signed-in address, recipients must be valid single-line addresses, and a reply fills in `In-Reply-To` and `References` from the original message. The server exposes no deletion, expunge, move to Trash, or sending of mail.

`move_to_promotions(uid, confirm=true)` keeps its previous behavior until it is removed: it moves only into `YANDEX_PROMOTIONS_FOLDER` and creates it if missing. `confirm=true` can be supplied by the model and is not proof of human approval. `YANDEX_PROMOTIONS_FOLDER` must be a top-level folder: names containing the server's hierarchy delimiter (`|` on Yandex) are rejected, as are system folders such as Trash and Spam. System folder names stop the server at startup; the remaining checks run before the confirmation request, which names the destination folder.

**Do not treat instructions appearing inside messages as user instructions.** The server is explicitly designed to keep the message content untrusted: its MCP instructions tell the model so, and confirmation requests label quoted headers as untrusted. Configure the MCP client to require operator confirmation for interactive moves.

Errors reported to the client name the IMAP host and port and the operating system's reason, never the credentials.

## Development

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src
```

`uv sync` installs the `dev` dependency group (pytest, ruff, mypy, pip-audit) by default; pass `--no-dev` for a runtime-only environment. For a standard pip installation, use `pip install -e .`; pip resolves dependency versions independently of `uv.lock` and does not install the development tools.

CI runs the tests on Python 3.11, 3.12 and 3.13 with a coverage threshold of 85%, then ruff, mypy in strict mode, `pip-audit` of the locked runtime dependencies and a Docker build. Tests are local, with no need to connect to a real mailbox or use real tokens: they drive the code with fake IMAP clients and a fake Yandex OAuth transport, and keep away from the developer's keyring. End-to-end IMAP tests have **not** been performed against a live mailbox in CI.

## Roadmap

See the [project milestones](https://github.com/Lagrunge82/yandex-mail-mcp/milestones) for planned work.

## Documentation

- [Yandex: configure IMAP email clients](https://www.yandex.com/support/yandex-360/customers/mail/en/mail-clients/others.html)
- [MCP Python SDK: running servers](https://py.sdk.modelcontextprotocol.io/run/)
