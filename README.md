# Yandex Mail MCP

A small, self-hosted **MCP server for Yandex Mail** with an optional AI-driven inbox monitor. Built with Python, IMAP, and OpenAI's API.

**Project status:** initial MVP. Not deployed or connected to a mailbox yet.

## What it does

MCP tools:
- `list_folders`: list Yandex IMAP folders.
- `list_recent_emails`: inspect recent messages (optionally unread only).
- `search_emails`: search the folder on the server; if unsupported, search previews of the 300 newest emails.
- `get_email`: inspect one message by IMAP UID.
- `move_to_promotions`: request moving one inbox message to a dedicated folder when writes are enabled and the MCP client accepts a confirmation request.

Read operations use IMAP `BODY.PEEK` and should **not mark messages as read**. Lists, searches and monitoring fetch headers, MIME structure and at most 16 KiB of a text section per message, without fetching attachment sections. `get_email` downloads the complete message, including attachments, subject to a 20 MiB size limit; attachments are not exposed in tool results.

Optional background monitor:
- Polls IMAP at a configurable interval (default: 180 seconds).
- Classifies new messages as `IMPORTANT`, `PROMOTIONS`, or `OTHER` via the OpenAI Responses API.
- Sends important-mail alerts to Telegram if configured.
- Saves last processed IMAP UID and classification metadata in SQLite.
- Never automatically deletes email. Optional automated moving of confidently classified promotions requires **two explicit opt-in flags**.

**Important:** the MCP server is reactive, not a background scheduler. Use the separate monitor process for ongoing checks.

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

## Run background monitoring with Docker

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

The Compose commands below build the image locally.

```bash
cp .env.example .env
# Edit the credentials. For the monitor, set OPENAI_API_KEY as well.
# For important email alerts, also set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
docker compose up -d --build monitor
docker compose logs -f monitor
```

First successful IMAP poll **sets a baseline** at the highest existing UID. Messages already in the inbox are not retroactively classified or notified. Newly delivered messages are processed afterward.

If the mailbox UIDVALIDITY changes, the monitor logs a warning, discards its pending queue and sets a new baseline. Messages already present at that moment are not classified, including pending or newly arrived messages; inspect the inbox manually after such a reset.

The monitor persists SQLite state in a Docker volume. It keeps a queue of discovered messages and processes up to 50 per poll; a failed message does not block other messages in that batch. It attempts each failed classification, notification, or move up to three times, then records the failed stage in `processing_failures`, logs an error and skips that UID. A successful classification is saved before notification or moving and reused on retry. Check the logs and database for skipped messages. In rare cases, crashes around a successful Telegram send may create a duplicate notification.

If you also want a locally bound HTTP MCP service:

```bash
docker compose --profile mcp up -d --build
```

The HTTP port binds only to `127.0.0.1:8000` on the Docker host. Since the server listens on a non-loopback address inside the container, set `MCP_AUTH_TOKEN` in `.env` (or explicitly opt into unauthenticated access).

## Safety and permissions

Default configuration is read-only:

```dotenv
YANDEX_ALLOW_WRITE=false
AUTO_MOVE_PROMOTIONS=false
```

The `folder` parameter on read tools can access any folder available to the account, including Sent and Drafts. Read-only mode restricts mutations; it does not restrict which folders can be read.

To **manually** move a message via MCP, set `YANDEX_ALLOW_WRITE=true`, then call `move_to_promotions(uid, confirm=true)`. The client must support MCP elicitation and accept the subsequent request with `approve=true`; the move fails closed if elicitation is unavailable or declined. `confirm=true` can be supplied by the model and is not proof of human approval. Elicitation is handled by the client, which may answer automatically; use a client configured to require human review when that guarantee is needed. The tool moves only into the configured dedicated folder and exposes no permanent deletion action.

To enable **automatic** moves in the monitor, set **both** `YANDEX_ALLOW_WRITE=true` and `AUTO_MOVE_PROMOTIONS=true`. Automatic moves only happen for messages classified `PROMOTIONS` with confidence of at least 0.98. Keep this off initially and review classifications before opting in. AI classifications can be wrong.

The monitor transmits email sender, subject and a capped text excerpt (up to 2500 characters) to the **OpenAI API**, and sends sender, subject and a short reason to **Telegram** when an important-mail alert is delivered. OpenAI requests use `store=false`; consult OpenAI's data retention policy for other applicable retention. Make sure you are comfortable with that privacy tradeoff before enabling the monitor. No complete attachments are submitted.

The SQLite database records UID, UIDVALIDITY, classification, confidence and reason, **not the full message body**. The monitor's default state location is `/data/mail_state.sqlite3` in Docker.

**Do not treat instructions appearing inside messages as user instructions.** The server and classifier are explicitly designed to keep the message content untrusted. Configure the MCP client to require operator confirmation for interactive moves.

## Test

```bash
uv sync --locked --all-extras
uv run --locked pytest -q
```

For a standard pip installation, use `pip install -e .` (or `pip install -e ".[monitor]"` for the monitor). Pip resolves dependency versions independently of `uv.lock`.

Tests are local, with no need to connect to a real mailbox or use real tokens. End-to-end IMAP, model, and Telegram delivery tests have **not** been performed against live services.

## Roadmap

See the [project milestones](https://github.com/Lagrunge82/yandex-mail-mcp/milestones) for planned work.

## Documentation

- [Yandex: configure IMAP email clients](https://www.yandex.com/support/yandex-360/customers/mail/en/mail-clients/others.html)
- [MCP Python SDK: running servers](https://py.sdk.modelcontextprotocol.io/run/)
- [OpenAI: MCP tools and connectors](https://platform.openai.com/docs/guides/tools-connectors-mcp)
