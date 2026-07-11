# Private Runtime Directory

Create `private/runtime.env` from `runtime.env.example`, then set:

```bash
chmod 700 private
chmod 600 private/runtime.env
```

`private/` is ignored as a whole. It contains live credentials, generated
account passwords, OAuth tokens, batch manifests, logs, bundles, and the one
Sub2API backup created for each imported batch. Never force-add it to Git.

The public repository contains only this empty template. Run this check before
every push:

```bash
git ls-files | grep -E '(^|/)(private|\.env)(/|$)' && exit 1 || true
```
