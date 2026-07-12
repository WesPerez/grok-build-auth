# Local Credential Inventory

The real copy belongs at `private/SECRETS.md` with mode `0600`.

Record the purpose, owner, source, rotation date, and corresponding key in
`private/runtime.env` for:

- YesCaptcha API key
- IMAP/Mailu password
- authenticated HTTP/SOCKS proxy URL
- Sub2API deployment environment location
- Sub2API admin/deployment credential and Grok group API key
- bridge management credential
- reverse-proxy or Web console Basic Auth credential

For each item also record its rotation date, revocation status, and the service
that must be restarted after rotation. Do not put the secret value in this
public template.

Do not copy secret values into public documentation, issues, logs, or commits.
