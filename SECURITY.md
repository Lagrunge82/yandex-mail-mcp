# Security policy

## Supported versions

This project is an early-stage self-hosted tool (0.x). Only the latest release receives fixes: check the [releases](https://github.com/Lagrunge82/yandex-mail-mcp/releases) and the repository for available fixes. No support or response-time commitment is currently specified.

## Reporting a vulnerability

Please do not post credentials, message contents, or an exploitable vulnerability in a public issue. Use [GitHub private vulnerability reporting](https://github.com/Lagrunge82/yandex-mail-mcp/security/advisories/new) if it is enabled. If it is unavailable, contact a repository maintainer privately before sharing technical details.

Include the affected version, a description of the impact, and steps to reproduce using test data. Never include real mailbox passwords, API tokens, or private mail. Maintainers can coordinate a fix and disclosure through the private report.

## Security model

The README's [Safety and permissions](README.md#safety-and-permissions) section describes the guarantees the server is designed to keep. In short:

- The server is read-only unless `YANDEX_ALLOW_WRITE=true`, and never deletes, expunges or sends mail.
- Each change acts on one message and is confirmed by the user through MCP elicitation; the reversible flag and draft changes can be exempted by the operator.
- Mail contents are treated as untrusted data: headers quoted in confirmation requests are labelled as untrusted, and the server instructs the model never to act on instructions found in mail.
- Credentials are read from the environment or `.env`, are never logged or returned in errors, and the IMAP connection always uses TLS with certificate verification.
- The HTTP transport requires a bearer token on non-loopback addresses unless explicitly disabled, and enables DNS rebinding protection.

A report that shows one of these guarantees broken is a security issue, even if it needs a malicious email to trigger.
