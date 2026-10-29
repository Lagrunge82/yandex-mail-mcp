# Security policy

## Supported versions

This project is an early-stage self-hosted tool (0.x). Only the latest release receives fixes: check the [releases](https://github.com/Lagrunge82/yandex-mail-mcp/releases) and the repository for available fixes. No support or response-time commitment is currently specified.

## Reporting a vulnerability

Please do not post credentials, message contents, or an exploitable vulnerability in a public issue. Use [GitHub private vulnerability reporting](https://github.com/Lagrunge82/yandex-mail-mcp/security/advisories/new) if it is enabled. If it is unavailable, contact a repository maintainer privately before sharing technical details.

Include the affected version, a description of the impact, and steps to reproduce using test data. Never include real mailbox passwords, API tokens, or private mail. Maintainers can coordinate a fix and disclosure through the private report.

## Security model

The README's [Safety and permissions](README.md#safety-and-permissions) and [Security](README.md#security) sections describe the guarantees the server is designed to keep. In short:

- The server is read-only unless `YANDEX_ALLOW_WRITE=true`, and never deletes, expunges or sends mail.
- Each change acts on one message and is confirmed by the user through MCP elicitation; the reversible flag and draft changes can be exempted by the operator.
- Mail contents are treated as untrusted data: headers quoted in confirmation requests are labelled as untrusted, and the server instructs the model never to act on instructions found in mail.
- The local server signs in with a Yandex OAuth token kept in the system keyring (or a 0600 file); credentials are never logged or returned in errors, and the IMAP connection always uses TLS with certificate verification.
- The shared HTTP server stores no user tokens, lets only the logins in `MCP_ALLOWED_USERS` sign in, and enables DNS rebinding protection; a non-loopback HTTP server refuses to start without `MCP_PUBLIC_URL`.

A report that shows one of these guarantees broken is a security issue, even if it needs a malicious email to trigger.

## Secrets of a shared HTTP server

A server run with `MCP_PUBLIC_URL` stores no user passwords or tokens. It holds two secrets of its own:

- `MCP_TOKEN_ENCRYPTION_KEYS`: the AES-256-GCM key that seals every token the server issues. Each token carries the user's Yandex access or refresh token inside, so **a compromised key together with an intercepted token gives access to that user's mail**. Keep the key in secret storage (an environment secret, not a committed file), serve the server only over HTTPS, and rotate the key if it may have leaked: removing the old key ends every token sealed with it.
- `YANDEX_OAUTH_CLIENT_SECRET`: the secret of the Yandex OAuth application, which the server needs to exchange codes and refresh tokens. A leaked secret lets someone impersonate the application at Yandex; reset it in the Yandex OAuth console.

Users can revoke their own access at any time with the `logout` tool or in their Yandex ID settings; operators decide who may sign in with `MCP_ALLOWED_USERS`.
