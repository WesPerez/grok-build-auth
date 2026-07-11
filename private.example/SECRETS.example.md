# Local Credential Inventory

The real copy belongs at `private/SECRETS.md` with mode `0600`.

Record the purpose, owner, source, rotation date, and corresponding key in
`private/runtime.env` for:

- YesCaptcha API key
- IMAP/Mailu password
- authenticated HTTP/SOCKS proxy URL
- Sub2API deployment environment location

Do not copy secret values into public documentation, issues, logs, or commits.
