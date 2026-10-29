# Yandex Mail MCP

A small, self-hosted **MCP server for Yandex Mail**. Built with Python and IMAP.

**Project status:** initial MVP. Not deployed or connected to a mailbox yet.

## What it does

MCP tools:
- `list_folders`: list Yandex IMAP folders.
- `list_recent_emails`: inspect recent messages (optionally unread only).
- `search_emails`: search the folder on the server; if unsupported, search previews of the 300 newest emails.
- `get_email`: inspect one message by IMAP UID.
- `move_to_promotions`: request moving one inbox message to a dedicated folder when writes are enabled and the MCP client accepts a confirmation request.

Read operations use IMAP `BODY.PEEK` and should **not mark messages as read**. Lists and searches fetch headers, MIME structure and at most 16 KiB of a text section per message, without fetching attachment sections. `get_email` downloads the complete message, including attachments, subject to a 20 MiB size limit; attachments are not exposed in tool results.

## Yandex setup

1. Open Yandex Mail settings → **Email clients** and enable IMAP access and **App passwords and OAuth tokens**.
2. Create a dedicated **Mail app password** in Yandex ID. Do not use your primary account password.
3. Copy `.env.example` to `.env`. Set `YANDEX_EMAIL` and `YANDEX_APP_PASSWORD`.
4. Never commit, paste into an issue, or upload the populated `.env` file. It is gitignored.

Yandex's IMAP settings: `imap.yandex.com:993` (SSL). The server does not use SMTP or send mail.

## Run the MCP server locally

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/) for the locked development setup.

```bash
uv sync --locked
cp .env.example .env
# Edit .env with your actual credentials
uv run yandex-mail-mcp
```

The default transport is **stdio**, suitable for local MCP clients. For a local HTTP MCP endpoint:

```bash
MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT=8000 uv run yandex-mail-mcp
```

The endpoint is `http://127.0.0.1:8000/mcp`. Set `MCP_AUTH_TOKEN` to require a bearer token. Non-loopback binds require a token, unless `MCP_ALLOW_UNAUTHENTICATED_REMOTE=true` is explicitly set. DNS rebinding protection is enabled. A specific `MCP_HOST` is allowed by default, but wildcard binds such as `0.0.0.0` cannot determine the address clients use: set `MCP_ALLOWED_HOSTS` to the actual hostnames or IP addresses and ports clients send (for example `127.0.0.1:*,192.168.1.5:*`), and configure `MCP_ALLOWED_ORIGINS` for browser origins when needed. Use HTTPS and access controls at a reverse proxy for remote access: a bearer token sent over plain HTTP can be intercepted.

`GET /healthz` answers `200 ok` while the HTTP server is serving. It does not log in to IMAP and does not require the bearer token. The stdio transport has no health endpoint.

## Run with Docker

CI publishes `ghcr.io/lagrunge82/yandex-mail-mcp` for Linux amd64 and arm64 after
the checks pass on `main` or a `v*` tag. When a push to `main` increases
`project.version` in `pyproject.toml`, CI creates the matching Git tag (for
example `v0.1.1`) and publishes the image with that tag. A conflicting existing
Git tag fails the workflow instead of being moved. The GitHub Actions token does
not trigger another workflow for the new tag, so the same run publishes the
versioned image. Versions must be valid Docker tags (PEP 440 local versions with
`+` are unsupported). The `main` image also has the `latest` and commit-SHA tags;
manually pushed `v*` tags produce matching image tags. Each publication
also gets a unique `build-<run_id>-<run_attempt>` tag. Registry tags can still be
overwritten; for a strictly immutable deployment, use the `image@sha256:...` digest
reference recorded in the workflow run summary. Package visibility
is managed in GitHub Packages; access to a private image requires registry login.
For example, after the first publication:

```bash
docker pull ghcr.io/lagrunge82/yandex-mail-mcp:latest
```

The image runs `yandex-mail-mcp` with the stdio transport by default, for example
`docker run -i --rm --env-file .env ghcr.io/lagrunge82/yandex-mail-mcp:latest`.

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

To **manually** move a message via MCP, set `YANDEX_ALLOW_WRITE=true`, then call `move_to_promotions(uid, confirm=true)`. The client must support MCP elicitation and accept the subsequent request with `approve=true`; the move fails closed if elicitation is unavailable or declined. `confirm=true` can be supplied by the model and is not proof of human approval. Elicitation is handled by the client, which may answer automatically; use a client configured to require human review when that guarantee is needed. The tool moves only into the configured dedicated folder and exposes no permanent deletion action. `YANDEX_PROMOTIONS_FOLDER` must be a top-level folder: names containing the server's hierarchy delimiter (`|` on Yandex) are rejected, as are system folders such as Trash and Spam. System folder names stop the server at startup; the remaining checks run before the confirmation request, which names the destination folder.

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
