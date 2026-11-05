# Contributing

Let's Plan Soccer's backend is a small hobby project that Craig Johnson runs. Issues and pull requests are welcome.

## Before you start

- The page people use, sign-in and Member access belong to [The Lobby](https://github.com/CraigDevJohnson/the-lobby). This repository is only the backend the site calls.
- The README is the specification: it says what each route does and why. A pull request that changes what the backend does updates the README too.
- For anything larger than a small fix, open an issue first (bug report, feature request or question) so the shape can be agreed before the work.

## Working on it

You need Go (the version in `go.mod`), [Task](https://taskfile.dev), and OpenTofu 1.13 for the checks of `infra/`.

```sh
task run      # serve on http://127.0.0.1:8081
task ci       # gofmt, vet, the Go tests and the offline OpenTofu checks
task lambda   # build dist/soccer.zip, as CI and the deploy do
```

A bug fix starts with a test that fails without it.

## Pull requests

- Branch from `main` and keep each pull request to one topic.
- CI must pass: its `Go` and `Infrastructure` jobs run on every pull request and never touch AWS. Codex reviews every pull request too.
- Merge with Squash and merge. A merge to `main` that changes what is deployed deploys to dev, and production waits for Craig's approval; a docs-only merge deploys nothing ([docs/operations/deploy.md](docs/operations/deploy.md)).
- Never commit secrets, state, plans, or tfvars outside `infra/environments/`. The `.gitignore` keeps most of them out.
- GitHub Actions are pinned to a commit SHA with the version in a comment. Dependabot checks the Actions, the Go modules and the OpenTofu providers weekly and updates the SHA and the comment together.

## Commit messages

Match the history: one short sentence in the imperative that says what changes, written for someone who has not read the code, for example "Keep the event description in the league's Home vs Away order". When the reason is not obvious, the body says why.

## Labels

| Label | Meaning |
|---|---|
| `bug`, `enhancement`, `question` | What kind of issue it is; the issue forms set these |
| `documentation` | The README or another doc is wrong or missing something; added by hand |
| `P0` | Emergency: the backend is down, loses a Team's games, or leaks something private |
| `P1` | High: breaks something people use; fix soon |
| `P2` | Medium: normal priority |
| `P3` | Low: nice to have |
| `dependencies` | Set by Dependabot on its pull requests |

## Security

Never open a public issue for a security problem. [SECURITY.md](SECURITY.md) says how to report one.
