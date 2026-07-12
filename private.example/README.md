# Private Runtime Directory

Create `private/runtime.env` from `runtime.env.example`, then set:

```bash
chmod 700 private
chmod 600 private/runtime.env
```

`private/` is ignored as a whole. It contains live credentials, generated
account passwords, OAuth tokens, batch manifests, logs, bundles, and Sub2API
recovery points. A new import or reconcile attempt may create a new backup,
including after a resume. Never force-add it to Git.

The public repository contains only this empty template. Run this check before
every push:

```bash
git ls-files | rg '(^|/)(private|\.env)(/|$)' && exit 1 || true
```
