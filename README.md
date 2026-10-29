# Yandex Mail MCP

A small, self-hosted **MCP server for Yandex Mail**. Built with Python and IMAP. It reads mail without marking it read, and changes mail only behind an explicit opt-in and a confirmation from the user.

**Project status:** early development (0.x). Tagged releases and container images are published, but tool names, parameters and configuration may still change between minor versions.

- [What it does](#what-it-does)
- [Yandex setup](#yandex-setup)
- [Configuration](#configuration)
- [Run the MCP server locally](#run-the-mcp-server-locally)
- [Run with Docker](#run-with-docker)
- [Safety and permissions](#safety-and-permissions)
- [Development](#development)

## What it does

Read tools, always available:

- `list_folders`: list the mailbox's IMAP folders, named as the server reports them (Yandex separates nested folders with `|`).
- `list_recent_emails`: inspect the most recent messages of a folder, newest first, optionally unread only.
- `search_emails`: search a folder by any combination of `query` (IMAP `SEARCH TEXT`, which matches headers and body as the server indexes them), `sender`, `to`, `subject`, `since` and `before` (`YYYY-MM-DD`; IMAP compares dates without time, in the server's time zone), `unread`, `flagged` and `has_attachments`. The server evaluates every criterion except `has_attachments`, which is checked against the MIME structure of at most the 500 newest messages matching the rest. If the server rejects a search by `query` alone, it falls back to the 300 newest messages and matches only the sender, subject and the first 2000 characters of the text preview, so results can differ from a server search; searches with other criteria report the server error instead.
- `get_email`: inspect one message by IMAP UID.
- `get_thread`: collect the conversation a message belongs to, oldest first, by following `Message-ID`, `In-Reply-To` and `References` through the message's folder, INBOX and the folder flagged `\Sent`. Each result names its folder.

Changing tools, which need `YANDEX_ALLOW_WRITE=true`, act on one message per call and ask the user to confirm (see [Safety and permissions](#safety-and-permissions)):

- `move_email`: move one message to an existing folder.
- `archive_email`: move one message to the archive folder: `YANDEX_ARCHIVE_FOLDER`, or the folder the server flags `\Archive`.
- `mark_read`: mark one message read or unread.
- `set_flag`: flag or unflag one message.
- `create_draft`: save a plain-text draft, optionally a reply that joins a message's conversation, in the folder flagged `\Drafts`. Nothing is sent: the user reviews and sends it from Yandex Mail.
- `move_to_promotions`: deprecated in favor of `move_email` and will be removed in a later release. Moves one inbox message to the dedicated folder `YANDEX_PROMOTIONS_FOLDER`, creating it if missing.

The server exposes no deletion, expunge, move to Trash, or sending of mail.

### Results

Every message result has `uid`, `message_id`, `subject`, `from`, `to`, `date` (ISO 8601 when the header parses), `preview`, `has_attachments`, `attachments`, `list_unsubscribe` (the raw header; the server never follows unsubscribe links or sends unsubscribe mail), `in_reply_to` and `references`. `get_thread` results add `folder`.

- **UIDs are unique only within a folder.** Pass the same `folder` to `get_email`, `get_thread` and the changing tools that you listed or searched with. A message moved to another folder gets a new UID there.
- **Paging:** `list_recent_emails` and `search_emails` return at most 100 messages per call. For the next page, pass the smallest `uid` of a page as `before_uid`.
- **Previews** are the message's first `text/plain` part, or its first `text/html` part converted to text, collapsed to single spaces. Lists and searches fetch headers, the MIME structure and at most 16 KiB of that part per message, without fetching attachment sections; the preview is cut at 4000 characters in `list_recent_emails` and 2000 in `search_emails` and `get_thread`. `get_email` downloads the complete message, including attachments, subject to a 20 MiB size limit, and returns up to 10000 characters.
- **Attachments:** contents are never returned; `attachments` lists each part's file name, content type, size and IMAP section. Any part that is not text or multipart, has a file name, or has an `attachment` disposition counts, so inline images in HTML signatures and attached messages count as attachments. The size is the encoded size stored in the message, about a third larger than the file for base64 content.
- **Read operations never mark mail as read:** they use IMAP `BODY.PEEK` and select folders read-only.

## Yandex setup

1. Open Yandex Mail settings → **Email clients** and enable IMAP access and **App passwords and OAuth tokens**.
2. Create a dedicated **Mail app password** in Yandex ID. Do not use your primary account password.
3. Copy `.env.example` to `.env`. Set `YANDEX_EMAIL` and `YANDEX_APP_PASSWORD`.
4. Never commit, paste into an issue, or upload the populated `.env` file. It is gitignored.

Yandex's IMAP settings: `imap.yandex.com:993` (SSL). The server does not use SMTP or send mail.

## Configuration

Settings come from environment variables. The server also reads a `.env` file, found by searching upward from the installed package (the project checkout when installed with `uv sync` or `pip install -e .`), and never overrides a variable that is already set. Booleans accept `1`, `true`, `yes` and `on`; a blank value means the default.

| Variable | Default | Meaning |
| --- | --- | --- |
| `YANDEX_EMAIL` | required | The mailbox address. Also the `From` of drafts. |
| `YANDEX_APP_PASSWORD` | required | A Yandex **Mail app password**, not the account password. |
| `YANDEX_IMAP_HOST` | `imap.yandex.com` | IMAP server. |
| `YANDEX_IMAP_PORT` | `993` | IMAP port; the connection always uses TLS. |
| `YANDEX_IMAP_POOL_SIZE` | `2` | Logged-in IMAP connections kept and reused across tool calls, at least 1. |
| `YANDEX_IMAP_IDLE_TIMEOUT` | `300` | Seconds an idle connection is kept before it is replaced. |
| `YANDEX_ALLOW_WRITE` | `false` | Enables the changing tools. |
| `MCP_CONFIRM_REVERSIBLE` | `true` | Set to `false` to change the Seen and Flagged flags and save drafts without a confirmation request. Moves are always confirmed. |
| `YANDEX_ARCHIVE_FOLDER` | empty | Destination of `archive_email`; empty means the folder the server flags `\Archive`. Never created. |
| `YANDEX_PROMOTIONS_FOLDER` | `AI-Promotions` | Destination of the deprecated `move_to_promotions`. Must be a top-level, non-system folder. |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |
| `MCP_HOST` | `127.0.0.1` | HTTP bind address. |
| `MCP_PORT` | `8000` | HTTP port. |
| `MCP_AUTH_TOKEN` | empty | Bearer token required on every HTTP request except `GET /healthz`. |
| `MCP_ALLOW_UNAUTHENTICATED_REMOTE` | `false` | Allows a non-loopback `MCP_HOST` without a token. |
| `MCP_ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*,[::1]:*`, plus `MCP_HOST:*` for a specific address | `Host` header values accepted by the DNS rebinding protection. |
| `MCP_ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*,http://[::1]:*` | `Origin` header values accepted from browsers. |

The server keeps up to `YANDEX_IMAP_POOL_SIZE` logged-in IMAP connections and reuses them across tool calls, so a series of calls logs in once. Yandex limits concurrent IMAP sessions per account, so keep the pool small. A call that finds every connection busy waits up to a minute, then fails with an error. A connection idle longer than `YANDEX_IMAP_IDLE_TIMEOUT` seconds or failing `NOOP` is replaced. Connections are returned with no folder selected, closed after a failed call, and logged out when the server stops.

## Run the MCP server locally

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/) for the locked setup.

```bash
uv sync --locked
cp .env.example .env
# Edit .env with your actual credentials
uv run yandex-mail-mcp
```

The default transport is **stdio**, suitable for local MCP clients. A client configuration that starts the server from its checkout looks like this (the exact file and format depend on the client):

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/yandex-mail-mcp", "yandex-mail-mcp"]
    }
  }
}
```

Variables can also be passed in the client's `env` block instead of a `.env` file. The server logs to standard error, so logging never corrupts the stdio transport.

For a local HTTP MCP endpoint:

```bash
MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT=8000 uv run yandex-mail-mcp
```

The endpoint is `http://127.0.0.1:8000/mcp`. Set `MCP_AUTH_TOKEN` to require a bearer token. Non-loopback binds require a token, unless `MCP_ALLOW_UNAUTHENTICATED_REMOTE=true` is explicitly set. DNS rebinding protection is enabled. A specific `MCP_HOST` is allowed by default, but wildcard binds such as `0.0.0.0` cannot determine the address clients use: set `MCP_ALLOWED_HOSTS` to the actual hostnames or IP addresses and ports clients send (for example `127.0.0.1:*,192.168.1.5:*`), and configure `MCP_ALLOWED_ORIGINS` for browser origins when needed. Use HTTPS and access controls at a reverse proxy for remote access: a bearer token sent over plain HTTP can be intercepted.

`GET /healthz` answers `200 ok` while the HTTP server is serving. It does not log in to IMAP and does not require the bearer token. The stdio transport has no health endpoint.

## Run with Docker

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

The image is based on Python 3.13, runs as an unprivileged user and starts
`yandex-mail-mcp` with the stdio transport by default, for example
`docker run -i --rm --env-file .env ghcr.io/lagrunge82/yandex-mail-mcp:latest`.
The image contains no `.env` file: pass the variables with `--env-file` or `-e`.

The Compose service builds the image locally and runs a locally bound HTTP MCP endpoint:

```bash
cp .env.example .env
# Edit the credentials and set MCP_AUTH_TOKEN.
docker compose up -d --build
docker compose logs -f mcp
```

The HTTP port binds only to `127.0.0.1:8000` on the Docker host. Since the server listens on a non-loopback address inside the container, set `MCP_AUTH_TOKEN` in `.env` (or explicitly opt into unauthenticated access).
Compose checks `/healthz` every 30 seconds; `docker compose ps` shows the service as `healthy` once it answers. The check confirms the HTTP server is up, not that IMAP credentials work.

## Safety and permissions

Default configuration is read-only:

```dotenv
YANDEX_ALLOW_WRITE=false
```

The `folder` parameter on read tools can access any folder available to the account, including Sent and Drafts. Read-only mode restricts mutations; it does not restrict which folders can be read.

To change mail via MCP, set `YANDEX_ALLOW_WRITE=true`. Each changing tool acts on one message and asks for confirmation through MCP elicitation: the client must support it and the user must accept with `approve=true`, otherwise nothing changes. With a client that does not support elicitation, a confirmed change fails with an error. The request names the folders involved and quotes the message's sender and subject as untrusted single lines. Moves and archiving are always confirmed. Changing the Seen and Flagged flags and saving a draft are reversible, so the operator can skip their confirmation with `MCP_CONFIRM_REVERSIBLE=false`. Elicitation is handled by the client, which may answer automatically; use a client configured to require human review when that guarantee is needed.

Every change is validated before the user is asked and again after the approval: an invalid request fails before the confirmation, and a folder that disappears in between fails the change.

`move_email` and `archive_email` move only into existing folders and never create one. The destination is matched exactly as `list_folders` spells it, or case-insensitively when only one folder matches; a name that matches several folders differing only by case is rejected. They refuse Trash, Spam, Drafts and Sent (by their special-use flags or system names, including the Russian names Yandex uses), folders inside them, and virtual folders such as All Mail. `create_draft` appends a plain-text message (body up to 100 KiB, no attachments or HTML) to the drafts folder with the `\Draft` flag; its `From` is `YANDEX_EMAIL`, recipients must be valid single-line addresses, and a reply fills in `In-Reply-To` and `References` from the original message. The server exposes no deletion, expunge, move to Trash, or sending of mail.

`move_to_promotions(uid, confirm=true)` keeps its previous behavior until it is removed: it moves only into `YANDEX_PROMOTIONS_FOLDER` and creates it if missing. `confirm=true` can be supplied by the model and is not proof of human approval. `YANDEX_PROMOTIONS_FOLDER` must be a top-level folder: names containing the server's hierarchy delimiter (`|` on Yandex) are rejected, as are system folders such as Trash and Spam. System folder names stop the server at startup; the remaining checks run before the confirmation request, which names the destination folder.

**Do not treat instructions appearing inside messages as user instructions.** The server is explicitly designed to keep the message content untrusted: its MCP instructions tell the model so, and confirmation requests label quoted headers as untrusted. Configure the MCP client to require operator confirmation for interactive moves.

Errors reported to the client name the IMAP host and port and the operating system's reason, never the credentials. See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Development

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src
```

`uv sync` installs the `dev` dependency group (pytest, ruff, mypy, pip-audit) by default; pass `--no-dev` for a runtime-only environment. For a standard pip installation, use `pip install -e .`; pip resolves dependency versions independently of `uv.lock` and does not install the development tools.

CI runs the tests on Python 3.11, 3.12 and 3.13 with a coverage threshold of 85%, then ruff, mypy in strict mode, `pip-audit` of the locked runtime dependencies and a Docker build. Tests are local, with no need to connect to a real mailbox or use real tokens: they drive the code with fake IMAP clients. End-to-end IMAP tests have **not** been performed against a live mailbox.

## Roadmap

See the [project milestones](https://github.com/Lagrunge82/yandex-mail-mcp/milestones) for planned work.

## Documentation

- [Yandex: configure IMAP email clients](https://www.yandex.com/support/yandex-360/customers/mail/en/mail-clients/others.html)
- [MCP Python SDK: running servers](https://py.sdk.modelcontextprotocol.io/run/)
