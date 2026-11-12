# Changelog

What changed in the Let's Plan Soccer backend, newest first. There are no
releases: a merge to `main` deploys, so changes are listed under the day they
merged. Dependency updates are left out.

## 2026-11-12

- Add this changelog, the agent docs in `docs/agents/` and the triage labels.

## 2026-11-11

- Hold GitHub Actions updates from Dependabot for Craig's review (#24).
- Add the plan for the soccer archive migration and Lobby operations (#23).

## 2026-11-10

- Auto-merge Dependabot's minor and patch updates once the required checks pass (#22).

## 2026-11-05

- Answer the chosen games as Google Calendar event bodies on `POST /events` (#21).
- Serve the calendar behind a Member link that follows each Team into its next Session (#20).

## 2026-11-04

- Check dependencies weekly, the OpenTofu providers too, with a seven-day cooldown (#16).
- Add issue forms, a pull request template, and the contributor, security and agent guides (#15).

## 2026-10-29

- Keep the event description in the league's Home vs Away order (#12).

## 2026-10-27

- Put the person's Team first in every game title and end it with the field (#11).

## 2026-10-16

- Pin the deploy roles to the function role, their state prefix and log groups (#10).

## 2026-10-14

- Deploy the backend from GitHub Actions: dev on a merge to `main`, production after Craig's approval (#8).

## 2026-10-13

- Hand Session links on to the next Session, and find every Team (#7).

## 2026-10-12

- Rename the Schedule Downloader to Let's Plan Soccer (#6).

## 2026-10-11

- Deploy one backend per site environment with `task infra:* ENV=<env>` (#5).
- Add the OpenTofu for the Lambda and its Function URL, CI and the deploy runbook (#3).

## 2026-10-10

- Add the backend for the Visitor flow: Team search by name and division, Session links and the calendar download (#1).
