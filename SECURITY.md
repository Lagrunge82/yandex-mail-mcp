# Security policy

## Reporting a vulnerability

Please do not post credentials, message contents, or an exploitable vulnerability in a public issue. Use [GitHub private vulnerability reporting](https://github.com/Lagrunge82/yandex-mail-mcp/security/advisories/new) if it is enabled. If it is unavailable, contact a repository maintainer privately before sharing technical details.

Include the affected version, a description of the impact, and steps to reproduce using test data. Never include real mailbox passwords, API tokens, or private mail. Maintainers can coordinate a fix and disclosure through the private report.

This project is an early-stage self-hosted tool. Check the latest release and repository for available fixes; no support or response-time commitment is currently specified.

## Secrets of a shared HTTP server

A server run with `MCP_PUBLIC_URL` stores no user passwords or tokens. It holds two secrets of its own:

- `MCP_TOKEN_ENCRYPTION_KEYS`: the AES-256-GCM key that seals every token the server issues. Each token carries the user's Yandex access or refresh token inside, so **a compromised key together with an intercepted token gives access to that user's mail**. Keep the key in secret storage (an environment secret, not a committed file), serve the server only over HTTPS, and rotate the key if it may have leaked: removing the old key ends every token sealed with it.
- `YANDEX_OAUTH_CLIENT_SECRET`: the secret of the Yandex OAuth application, which the server needs to exchange codes and refresh tokens. A leaked secret lets someone impersonate the application at Yandex; reset it in the Yandex OAuth console.

Users can revoke their own access at any time with the `logout` tool or in their Yandex ID settings; operators decide who may sign in with `MCP_ALLOWED_USERS`.
