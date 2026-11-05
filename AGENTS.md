# AGENTS.md

Guidance for AI coding agents working in this repository.

## Start

- This is the Let's Plan Soccer backend, one Tool behind [The Lobby](https://github.com/CraigDevJohnson/the-lobby), which owns every screen and sign-in. Changes to the page belong there.
- Read `README.md` first. It is the specification: every route, the league's quirks, and the reason behind each choice. `docs/operations/deploy.md` covers deploying.
- Stack: Go with Chi, run on AWS Lambda (provided.al2023, arm64) through an adapter and as an ordinary server locally. OpenTofu in `infra/`. Every command is a Task task.
- `CONTRIBUTING.md` has the pull request, commit message and label conventions.

## Commands

- `task ci`: gofmt, vet, the Go tests and the offline OpenTofu checks. Needs no credentials; run it before pushing.
- `task test`, `task fmt`, `task vet`: one step each.
- `task run`: serve on http://127.0.0.1:8081.
- `task lambda`: build `dist/soccer.zip`, which CI also builds.
- `task infra:check`: the offline OpenTofu checks alone (needs OpenTofu 1.13).

## Rules

- A change to what the backend does updates the README in the same pull request.
- A bug fix starts with a test that fails without it.
- Change AWS only through GitHub Actions: a merge to `main` that changes what is deployed applies dev, and production waits for Craig's approval. What `docs/operations/deploy.md` says must run from a computer (`infra/bootstrap`, a first apply, a change to the function's role, removing a deployment) runs only after Craig approves that specific plan. `tofu apply`, `destroy`, `import`, `state` and `force-unlock` always need that approval; `task infra:init` and `task infra:plan` change nothing and do not. Craig's private `aws-setup` repository (`aws-design.md`) holds the AWS guardrails.
- Logs carry the route pattern, never the path, so Session link tokens and searches stay out. No answer or log line carries a coach id.
- Nothing about a Visitor is stored; a Session link's token carries the whole choice.
- Never commit secrets, state, plans, or tfvars outside `infra/environments/`.
- Pin a new GitHub Action to a commit SHA with its version in a comment.
- Merge pull requests with Squash and merge.
