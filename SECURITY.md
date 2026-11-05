# Security policy

## Reporting a problem

Please do not open a public issue for a security problem. Email privacy@craigdevjohnson.com instead, and include:

- what you found, and where (a route, a workflow, or the infrastructure in `infra/`)
- how to reproduce it
- what it lets someone do

This is a hobby project that one person runs, so replies are best effort, usually within a week. A fix's pull request credits the reporter unless they ask not to be named.

## What is supported

Only what runs from `main`. The backend deploys from `main`, so there are no older releases to patch.

## Scope

This repository's Go code, its GitHub Actions workflows, and its OpenTofu. The site, sign-in and Member access are [The Lobby](https://github.com/CraigDevJohnson/the-lobby)'s; the same address takes reports about them. The league's own schedule service belongs to Let's Play Soccer, not to this project.
