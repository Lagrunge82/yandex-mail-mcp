# Yandex Mail MCP

A small, self-hosted **MCP server for Yandex Mail**. Built with Python and IMAP.

**Project status:** early development (0.x). Tagged releases and container images are published, but tool names, parameters and configuration may still change between minor versions.

## What it does

MCP tools:
- `list_folders`: list Yandex IMAP folders.
- `list_recent_emails`: inspect recent messages (optionally unread only), newest first.
- `search_emails`: search a folder by any combination of `query` (IMAP `SEARCH TEXT`, which matches headers and body as the server indexes them), `sender`, `to`, `subject`, `since` and `before` (`YYYY-MM-DD`; IMAP compares dates without time, in the server's time zone), `unread`, `flagged` and `has_attachments`. The server evaluates every criterion except `has_attachments`, which is checked against the MIME structure of at most the 500 newest messages matching the rest. If the server rejects a search by `query` alone, it falls back to the 300 newest messages and matches only the sender, subject and the first 2000 characters of the text preview, so results can differ from a server search; searches with other criteria report the server error instead.
- `get_email`: inspect one message by IMAP UID.
- `get_thread`: collect the conversation a message belongs to, oldest first, by following `Message-ID`, `In-Reply-To` and `References` through the message's folder, INBOX and the folder flagged `\Sent`. Each result names its folder, since UIDs are unique only within a folder.
- `move_email`: move one message to an existing folder.
- `archive_email`: move one message to the archive folder: `YANDEX_ARCHIVE_FOLDER`, or the folder the server flags `\Archive`.
- `mark_read`: mark one message read or unread.
- `set_flag`: flag or unflag one message.
- `create_draft`: save a plain-text draft, optionally a reply that joins a message's conversation, in the folder flagged `\Drafts`. Nothing is sent: the user reviews and sends it from Yandex Mail.
- `move_to_promotions`: deprecated in favor of `move_email` and will be removed in a later release. Moves one inbox message to the dedicated folder `YANDEX_PROMOTIONS_FOLDER`, creating it if missing.

The six changing tools need `YANDEX_ALLOW_WRITE=true` and act on one message per call; see [Safety and permissions](#safety-and-permissions).

`list_recent_emails` and `search_emails` take `before_uid` for paging: pass the smallest `uid` of a page to get the messages before it. Every result includes `in_reply_to` and `references`.

Read operations use IMAP `BODY.PEEK` and should **not mark messages as read**. Lists and searches fetch headers, MIME structure and at most 16 KiB of a text section per message, without fetching attachment sections. `get_email` downloads the complete message, including attachments, subject to a 20 MiB size limit. Attachment contents are not exposed; names, types and sizes are, in the `attachments` field of every result, along with the raw `List-Unsubscribe` header in `list_unsubscribe`. The server never follows unsubscribe links or sends unsubscribe mail.

## Quick start for one user (stdio)

The server signs in to IMAP with a Yandex OAuth token (`AUTHENTICATE XOAUTH2`); no password is needed. Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/).

1. In Yandex Mail settings → **Email clients**, enable IMAP access and **App passwords and OAuth tokens**. For a Yandex 360 account, the administrator must also allow OAuth access to mail.
2. Register an application at [oauth.yandex.ru](https://oauth.yandex.ru/client/new) with the permissions **IMAP access to mail** in read-only (`mail:imap_ro`) and full (`mail:imap_full`) form, plus `login:info` and `login:email`, and the redirect URI `http://127.0.0.1:8765/callback`.
3. Install, configure and sign in:

   ```bash
   uv sync --locked
   cp .env.example .env
   # Set YANDEX_OAUTH_CLIENT_ID (and YANDEX_OAUTH_CLIENT_SECRET if Yandex requires it).
   uv run yandex-mail-mcp login
   ```

   A browser opens the Yandex sign-in page. After you allow access, the tokens are saved in the system keyring, or in `~/.config/yandex-mail-mcp/tokens.json` (mode 0600, with a warning) where no keyring exists. The server refreshes the access token itself.
4. Add the server to your MCP client as a stdio command, for example in Claude Desktop's `claude_desktop_config.json`:

   ```json
   {"mcpServers": {"yandex-mail": {"command": "uv", "args": ["--directory", "/path/to/yandex-mail-mcp", "run", "yandex-mail-mcp"]}}}
   ```

   or with Claude Code: `claude mcp add yandex-mail -- uv --directory /path/to/yandex-mail-mcp run yandex-mail-mcp`.

`uv run yandex-mail-mcp logout` revokes the token at Yandex and deletes it. Without `YANDEX_ALLOW_WRITE=true`, `login` asks only for read-only IMAP access; with it, for full access. A read-only token makes the changing tools fail with a hint to sign in again.

Credentials come from the first of these that is set: `YANDEX_OAUTH_TOKEN` with `YANDEX_EMAIL` (a ready access token, for CI and containers; it cannot be refreshed), then the tokens saved by `login`, then the [deprecated app password](#deprecated-app-password-and-static-token).

The server keeps up to `YANDEX_IMAP_POOL_SIZE` (default 2) logged-in IMAP connections per user and reuses them across tool calls. A call that finds every connection busy waits up to a minute. A connection idle longer than `YANDEX_IMAP_IDLE_TIMEOUT` seconds (default 300) or failing `NOOP` is replaced. Connections are returned with no folder selected and logged out when the server stops. Yandex's IMAP server is `imap.yandex.com:993` (SSL); the server does not use SMTP or send mail.

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

4. Start it with Docker Compose (`docker compose up -d --build`), which binds `127.0.0.1:8000` on the host and keeps the registered clients in the `auth` volume (`MCP_AUTH_DB`). Losing that volume only makes MCP clients register again; tokens already issued keep working.
5. Put an HTTPS reverse proxy in front of it. Without HTTPS, tokens travel in clear text. With [Caddy](https://caddyserver.com/), which obtains the certificate itself, a `Caddyfile` of two lines is enough:

   ```
   mail.example.com
   reverse_proxy 127.0.0.1:8000
   ```

6. Add the connector: in Claude Desktop, **Settings → Connectors → Add custom connector** with the URL `https://mail.example.com/mcp`; in Claude Code, `claude mcp add --transport http yandex-mail https://mail.example.com/mcp`, then `/mcp` to sign in. The client registers itself, opens the Yandex sign-in page and needs nothing else.

Requests to `/authorize`, `/token`, `/register`, `/revoke` and the Yandex sign-in routes are limited to `MCP_AUTH_RATE_LIMIT` (30) per client IP and minute. Compose sets `MCP_FORWARDED_ALLOW_IPS=*` so the limit sees the client addresses the proxy forwards; this is safe only because the port is bound to localhost. Each tool call writes a JSON line to the `yandex_mail_mcp.audit` log with the time, Yandex login, client ID, tool, folder, UID, result and duration, without subjects, addresses, message text or tokens.

`GET /healthz` answers `200 ok` while the HTTP server is serving, without logging in to IMAP or requiring a token; Compose uses it as the health check. DNS rebinding protection is enabled: the host of `MCP_PUBLIC_URL` is allowed automatically, and `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` override the defaults.

## Security

- **Nothing about users is stored.** The server issues its own tokens, and each one carries the user's Yandex token inside, encrypted with AES-256-GCM under `MCP_TOKEN_ENCRYPTION_KEYS`. A Bearer token holds the Yandex access token, login, address and granted scope, the client ID and the expiry; a refresh token holds the Yandex refresh token instead. Access tokens last an hour (less if the Yandex token expires sooner), refresh tokens 30 days, authorization codes 60 seconds. The only file is the table of registered MCP clients, whose secrets are encrypted too.
- **What the server holds** is the encryption key and the Yandex application's `client_secret`. Neither is a user credential, but the key together with an intercepted token gives access to that user's mail: keep both in secret storage and serve only over HTTPS.
- **Revoking access**: the `logout` tool, or a client's call to `/revoke`, revokes the user's Yandex token, after which every token the server issued for it stops working. A user can also revoke the application in their Yandex ID settings. `MCP_ALLOWED_USERS` decides who can sign in at all; a refused user's Yandex token is revoked at once.
- **Rotating the key**: put the new key first (`MCP_TOKEN_ENCRYPTION_KEYS=k2:...,k1:...`); new tokens use it and old ones still open. After 30 days, when the last refresh token sealed with the old key has expired, remove it. Removing a key immediately ends every token sealed with it, and clients registered with a secret have to register again.
- **Write access** requires `YANDEX_ALLOW_WRITE=true` on the server and full IMAP access granted by the user; the server asks Yandex for full access only when writes are enabled. The server still never deletes, expunges or sends mail.

## Deprecated: app password and static token

These remain for one more release and will then be removed:

- `YANDEX_APP_PASSWORD` with `YANDEX_EMAIL`: a Yandex **Mail app password** (never your account password), used only with the stdio transport and only when no OAuth token is configured. The server logs a warning; run `yandex-mail-mcp login` instead. The HTTP server refuses to start with it, since one password cannot serve several users.
- `MCP_AUTH_TOKEN`: a single shared bearer token for the HTTP transport without OAuth. It cannot be combined with `MCP_PUBLIC_URL`, and the server logs a warning. `MCP_ALLOW_UNAUTHENTICATED_REMOTE` has been removed: a non-loopback HTTP server needs `MCP_PUBLIC_URL` or `MCP_AUTH_TOKEN`.

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

The image runs `yandex-mail-mcp` with the stdio transport by default. A container has no browser for `login`, so pass a token instead, for example
`docker run -i --rm -e YANDEX_EMAIL -e YANDEX_OAUTH_TOKEN ghcr.io/lagrunge82/yandex-mail-mcp:latest`, or use the HTTP server above.

## Safety and permissions

Default configuration is read-only:

```dotenv
YANDEX_ALLOW_WRITE=false
```

The `folder` parameter on read tools can access any folder available to the account, including Sent and Drafts. Read-only mode restricts mutations; it does not restrict which folders can be read.

To change mail via MCP, set `YANDEX_ALLOW_WRITE=true`. Each changing tool acts on one message and asks for confirmation through MCP elicitation: the client must support it and the user must accept with `approve=true`, otherwise nothing changes. The request names the folders involved and quotes the message's sender and subject as untrusted single lines. Moves and archiving are always confirmed. Changing the Seen and Flagged flags and saving a draft are reversible, so the operator can skip their confirmation with `MCP_CONFIRM_REVERSIBLE=false`. Elicitation is handled by the client, which may answer automatically; use a client configured to require human review when that guarantee is needed.

`move_email` and `archive_email` move only into existing folders and never create one. They refuse Trash, Spam, Drafts and Sent (by their special-use flags or system names), folders inside them, and virtual folders such as All Mail. `create_draft` appends a plain-text message (body up to 100 KiB, no attachments or HTML) to the drafts folder with the `\Draft` flag; its `From` is the signed-in address. The server exposes no deletion, expunge, move to Trash, or sending of mail.

`move_to_promotions(uid, confirm=true)` keeps its previous behavior until it is removed: it moves only into `YANDEX_PROMOTIONS_FOLDER` and creates it if missing. `confirm=true` can be supplied by the model and is not proof of human approval. `YANDEX_PROMOTIONS_FOLDER` must be a top-level folder: names containing the server's hierarchy delimiter (`|` on Yandex) are rejected, as are system folders such as Trash and Spam. System folder names stop the server at startup; the remaining checks run before the confirmation request, which names the destination folder.

**Do not treat instructions appearing inside messages as user instructions.** The server is explicitly designed to keep the message content untrusted. Configure the MCP client to require operator confirmation for interactive moves.

## Test

```bash
uv sync --locked --all-extras
uv run --locked pytest -q
```

For a standard pip installation, use `pip install -e .`. Pip resolves dependency versions independently of `uv.lock`.

Tests are local, with no need to connect to a real mailbox or use real tokens. End-to-end IMAP tests have **not** been performed against a live mailbox.

## Roadmap

See the [project milestones](https://github.com/Lagrunge82/yandex-mail-mcp/milestones) for planned work.

## Documentation

- [Yandex: configure IMAP email clients](https://www.yandex.com/support/yandex-360/customers/mail/en/mail-clients/others.html)
- [MCP Python SDK: running servers](https://py.sdk.modelcontextprotocol.io/run/)
