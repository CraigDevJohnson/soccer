# Deploying the backend

The Let's Plan Soccer page (`/soccer` in [the-lobby](https://github.com/CraigDevJohnson/the-lobby)) calls this backend through a Lambda Function URL that accepts only the site's own role, with requests the site signs (the-lobby ADR 0006). `infra/` is the OpenTofu for one such deployment, and there is one deployment per site environment: `dev` for `dev.craigdevjohnson.com` and `prod` for the production site. Each is named for the site environment it serves, `soccer-<site_environment>-server`, because the site's permissions boundary lets its role call only backends named `*-<environment>-*`, and each has its own state, inputs and plan file. Nothing here is public: there is no API Gateway, no custom domain and no unsigned path in.

GitHub Actions deploys both. A computer is used only to test a change, and once, to make the roles GitHub signs in with.

## How a change gets deployed

**On a pull request,** the `CI` workflow runs `task ci`: gofmt, vet, the Go tests, the Lambda build, and the OpenTofu checks for both roots against a mocked provider. It has no secrets and no cloud role, so it cannot touch AWS. Nothing is planned on a pull request.

**On `main`,** a push that changes what is deployed (the Go code, `go.mod` and `go.sum`, `infra/` other than its tests and `infra/bootstrap`, `scripts/smoke.sh`, `Taskfile.yaml` or the workflow itself) starts the `Deploy` workflow. A change to docs alone deploys nothing.

1. **Build,** once. The Go tests run, `task lambda` makes `dist/soccer.zip`, and the zip is kept as a workflow artifact named after the commit.
2. **Deploy dev,** with no approval. The job signs in to AWS as `soccer-deployer-dev` through GitHub OIDC, runs `task infra:init` and plans with the build's zip (`task infra:plan:zip`), then applies that exact plan (`task infra:apply`). The plan's resource lines and totals are on the run's summary page, and the full plan is in the log; the backend has no secrets, so neither shows anything private. Then `task infra:smoke` runs two checks:
   - Through the site, `https://dev.craigdevjohnson.com/soccer/link/deploy-check.ics` must answer `404` with `invalid_link`. The site only checks a token's shape, so that answer came from this backend, over the site's signed call, and the league was never asked.
   - Straight at the Function URL, an unsigned `GET /healthz` must answer `403`.
3. **Deploy production,** after Craig approves it. Only after dev succeeded. The job waits for approval, then does the same against `prod` as `soccer-deployer-prod`, with the same zip, and runs the same checks against the production site.

One build, deployed in order: what reaches production is byte for byte what ran on dev. Two deploys of the same environment never overlap; a second run waits and is never cancelled part-way through an apply. A plan or apply that finds the state locked, by a plan on a computer for example, waits up to five minutes for the lock. The same source and Go toolchain always give the same zip (the build strips paths and commit stamps, and the archive carries a fixed time and mode), so a run whose Go code did not change plans `No changes.` for the function.

To deploy `main` again without a push (after the one-time setup, or after a failed run), open Actions, choose `Deploy`, press "Run workflow" on `main`. Runs from any other branch or tag do nothing.

### Approving production

When a `Deploy` run reaches the production job, it pauses: GitHub emails Craig and shows "Review deployments" at the top of the run. Read dev's plan summary on the same run, then approve or reject. Production gets the same change only when it was in step with dev before this run: if an earlier run's production job was rejected or failed, production also gets that run's commits, and their changes are in that run's dev summary. Rejecting ends the run with dev deployed and production untouched. That approval is the approved change aws-design.md G11 asks for; nobody else can give it.

### Rolling back

Revert the commit on `main` and push. The workflow builds the reverted code, deploys dev and waits for approval for production, like any other change, and `main` stays in step with what is deployed.

## What GitHub holds

No AWS keys and no secrets. Each deploy job signs in with a token GitHub issues for that one run, and each role trusts only jobs in its own environment of this repository.

| Name | Kind | What it is |
|---|---|---|
| `dev` | environment | Deployment branches: `main` only. No reviewers |
| `production` | environment | Deployment branches: `main` only. Required reviewer: Craig |
| `AWS_DEV_DEPLOYER_ROLE_ARN` | variable in `dev` | `arn:aws:iam::793680745829:role/soccer-deployer-dev` |
| `AWS_PROD_DEPLOYER_ROLE_ARN` | variable in `production` | `arn:aws:iam::793680745829:role/soccer-deployer-prod` |

Each variable is set in its environment only, never for the whole repository. GitHub creates an environment a workflow names if it does not exist yet, with no protection; such an environment has no variable, so a deploy job in it stops at its first step ("Check the environment was set up") and never signs in.

## What a deployment creates

| Resource | Name | Why |
| --- | --- | --- |
| Lambda function | `soccer-<env>-server` | The Go server from `task lambda`, `provided.al2023` on arm64, 256 MB, 10-second timeout, with `LINK_BASE_URL` and the optional `LEAGUE_*` settings from the README |
| Function URL | on the function | `AWS_IAM` auth; the site's `SOCCER_BACKEND_URL` |
| Two function permissions | `AllowSiteThroughFunctionUrl`, `AllowSiteInvokeThroughFunctionUrl` | `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` for the site's role alone, the second only through the URL |
| IAM role and inline policy | `soccer-<env>-server`, `logs` | Lambda's execution role, inside `soccer-server-boundary-<env>`; it may write its own log group and nothing else |
| Log group | `/aws/lambda/soccer-<env>-server` | The code's JSON log lines, kept 30 days |
| Metric filter and alarm | `soccer-<env>-server-league-unavailable` | Counts the code's `league_unavailable` lines and emails on the first in any five minutes |
| Alarm | `soccer-<env>-server-failing` | Lambda errors: a crash or a run that hit the timeout |

The inputs are committed, one file each per deployment: `infra/environments/<env>.tfvars` (the account, the site's role, where the site serves Session links, the alerts topic, the boundary and the site's cost tag) and `infra/environments/<env>.tfbackend` (the state, in the site's state bucket under `soccer/<env>/terraform.tfstate`, with the bucket's native lock file). None of it is secret, and keeping it in Git means a deploy can never undo a value only one computer knew. When the production site moves to `craigdevjohnson.com`, change `link_base_url` in `prod.tfvars` and merge: the function's environment changes, nothing else.

The site's role is the only principal the function grants a call to. AWS also lets a principal in the same account call the URL when its own identity policy allows `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`; the account's workload roles and the deploy roles carry neither (aws-design G7), and the administrator profile does. Unsigned requests get a 403.

Both alarms notify the workloads account's existing `alerts` topic in us-west-2, which already lets CloudWatch publish and emails Craig (aws-setup `aws-design.md`, section 7). Every resource carries `project = soccer` and the tags in `tags` (aws-design G10).

## The deploy roles

`infra/bootstrap` is a second OpenTofu root, applied by hand with the administrator profile, because the roles it makes are what the workflow signs in as. Its state is `soccer/bootstrap/terraform.tfstate` in the same bucket. Per deployment it makes:

- **`soccer-deployer-<env>`,** trusted only by GitHub's OIDC token for a job in this repository's `dev` or `production` environment (the exact `aud` and `sub`; the repository uses GitHub's immutable subject, `repo:CraigDevJohnson@42454849/soccer@1412575682:environment:<name>`). Its one inline policy allows what an apply of `infra/` needs and no more: its own state key and lock file, and the function, log group, metric filter, alarms and role named `soccer-<env>-*`. Reads are pinned to the same names wherever AWS allows it, so it cannot read another app's state or secrets; only log group and alarm settings, which hold none, are read account-wide. Four conditions keep the backend private and the role inside its own deployment whatever a commit says: a Function URL can only be created or changed with `AWS_IAM` auth, a call to the function can only be granted to `site-<env>-server`, an alarm can do nothing but notify the `alerts` topic (so an alarm on another app's metric can never stop or terminate its instance), and a role can only be created, given a policy or given a boundary while it carries the boundary below, which can never be taken off. The deploy role is named outside `soccer-<env>-*`, so it cannot change itself.
- **`soccer-server-boundary-<env>`,** the permissions boundary on the function's role: at most, write the deployment's own logs.

`task infra:check` tests all of this offline (`infra/bootstrap/tests`).

## One-time setup

Done once, before the first cloud deploy, on a computer with OpenTofu 1.13 or newer, [Task](https://taskfile.dev), the AWS CLI with the `workloads-admin` profile from aws-setup, and the GitHub CLI signed in as Craig. Only step 5 changes AWS, and it runs only after the plan in step 4 is approved (aws-design G11). The GitHub environments are set up before the roles exist, so no job outside `main` ever gets a role to sign in as. Run everything in the soccer checkout, in one terminal, on `main` after `git pull`, or, before the change that adds this setup is merged, on its branch. A `Deploy` run that starts before the setup is done stops at its first step and changes nothing; GitHub creates the `dev` environment it names without protection, and step 3 then sets it up.

1. **Sign in.**

   ```sh
   export AWS_PROFILE=workloads-admin
   aws sso login
   aws sts get-caller-identity --query Account --output text
   ```

   `aws sso login` opens the browser for the Identity Center approval and prints `Successfully logged into Start URL: …`. The last line prints `793680745829`.

2. **Check the repository's OIDC subject.**

   ```sh
   gh api repos/CraigDevJohnson/soccer/actions/oidc/customization/sub
   ```

   It prints the repository's subject settings; `use_immutable_subject` must be `true`, as it is for the-lobby, minecraft and foundry. If it is not, stop: the roles would trust a subject GitHub never issues for this repository.

3. **Make the GitHub environments.** The `dev` environment, deployable from `main` only, with its role:

   ```sh
   gh api -X PUT repos/CraigDevJohnson/soccer/environments/dev --input - <<'EOF'
   {"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   EOF
   gh api -X POST repos/CraigDevJohnson/soccer/environments/dev/deployment-branch-policies -f name=main -f type=branch
   gh variable set AWS_DEV_DEPLOYER_ROLE_ARN --env dev --repo CraigDevJohnson/soccer --body arn:aws:iam::793680745829:role/soccer-deployer-dev
   ```

   The `production` environment, the same with Craig as its required reviewer (`42454849` is his GitHub user id):

   ```sh
   gh api -X PUT repos/CraigDevJohnson/soccer/environments/production --input - <<'EOF'
   {"reviewers": [{"type": "User", "id": 42454849}], "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   EOF
   gh api -X POST repos/CraigDevJohnson/soccer/environments/production/deployment-branch-policies -f name=main -f type=branch
   gh variable set AWS_PROD_DEPLOYER_ROLE_ARN --env production --repo CraigDevJohnson/soccer --body arn:aws:iam::793680745829:role/soccer-deployer-prod
   ```

   Each `PUT` prints the environment as JSON, each `POST` prints `{"id":…,"node_id":…,"name":"main","type":"branch"}`, and each `gh variable set` prints `✓ Created variable … for CraigDevJohnson/soccer`. The same can be done under Settings, Environments. Then check both:

   ```sh
   gh api repos/CraigDevJohnson/soccer/environments --jq '.environments[] | {name, rules: [.protection_rules[].type]}'
   ```

   This prints `{"name":"dev","rules":["branch_policy"]}` and `{"name":"production","rules":["required_reviewers","branch_policy"]}`. Production without `required_reviewers` would deploy without asking; fix it before going on. A `Deploy` run before step 5 fails when it signs in, since the roles do not exist yet, and changes nothing.

4. **Plan the roles.**

   ```sh
   task infra:bootstrap:init
   task infra:bootstrap:plan
   ```

   The init prints `Successfully configured the backend "s3"!` and ends with `OpenTofu has been successfully initialized!`. The plan reads the account's GitHub OIDC provider, then ends in `Plan: 6 to add, 0 to change, 0 to destroy.`: `aws_iam_policy.server_boundary["dev"]` and `["prod"]`, `aws_iam_role.deployer["dev"]` and `["prod"]`, and `aws_iam_role_policy.deployer["dev"]` and `["prod"]`. Anything else is a stop condition. The plan is saved as `infra/bootstrap/bootstrap.tfplan` (gitignored).

5. **Apply** the plan you approved.

   ```sh
   task infra:bootstrap:apply
   ```

   This prints a `Creation complete` line per resource and ends with `Apply complete! Resources: 6 added, 0 changed, 0 destroyed.`, then the outputs: `deployer_role_arns` (`dev` and `prod`, `arn:aws:iam::793680745829:role/soccer-deployer-<env>`) and `server_boundary_arns`.

6. **Deploy.** Merge to `main`, or, if it is merged already, run `Deploy` by hand (Actions, `Deploy`, "Run workflow" on `main`). Dev deploys by itself; approve production when the run asks.

The first cloud deploy of each environment ships whatever `main` holds that was not applied yet. The move to Actions itself changes two things in place: the function's role goes inside its boundary (`aws_iam_role.server`), and the function's `filename` in the state becomes `../dist/soccer.zip` instead of the absolute path a computer gave (`aws_lambda_function.server`, which uploads the code again).

## Testing on a computer

The same tasks run on a computer with the administrator profile, to see what a change would do before it merges. Sign in as in step 1 above, then:

```sh
task infra:init             # once per checkout; ENV=prod for production
task infra:plan             # builds dist/soccer.zip and plans; ENV=prod for production
```

`task infra:init` prints `Successfully configured the backend "s3"!` and ends with `OpenTofu has been successfully initialized!`; its provider cache is `infra/.terraform.<env>` (gitignored). `task infra:plan` prints the zip's `ls -l` line, then the plan, saved as `infra/soccer.<env>.tfplan`. A plan changes nothing; it only takes and releases the state lock. The zip's path in the state is relative to `infra/`, so a plan on a computer and one in the workflow do not differ over where the zip was built; on `main`, after a green `Deploy` run and with the Go version in `go.mod`, the zip is the same too and the plan reads `No changes.`

Applying from a computer (`task infra:apply`, then `task infra:smoke`) is for when GitHub Actions cannot deploy, and like any apply it needs Craig's approval of that plan first. The next `Deploy` run brings the environment back to `main` either way.

A bare `tofu` command needs the deployment's cache on its own command line, such as `TF_DATA_DIR="$PWD/infra/.terraform.dev" tofu -chdir=infra output -raw function_url`; do not `export` it, because every later bare `tofu` command in that terminal would then use that deployment's state, whichever you meant. The `task infra:*` commands set their own and are not affected. A checkout that deployed before the inputs were committed has gitignored `infra/backend.<env>.hcl` and `infra/deployment.<env>.tfvars` files; they are no longer read and can be deleted.

To call the backend yourself, sign the request with the administrator profile:

```sh
url=$(TF_DATA_DIR="$PWD/infra/.terraform.dev" tofu -chdir=infra output -raw function_url)
eval "$(aws configure export-credentials --profile workloads-admin --format env)"
curl -sS --aws-sigv4 "aws:amz:us-west-2:lambda" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" -H "x-amz-security-token: $AWS_SESSION_TOKEN" \
  "${url%/}/divisions" | head -c 200; echo
```

This prints the start of `{"divisions":[{"id":"…","name":"…"`: the function reached the league and read the divisions at Boise. The exported credentials expire with the SSO session.

## Removing a deployment

From a computer, as an approved change: `TF_DATA_DIR="$PWD/infra/.terraform.dev" tofu -chdir=infra plan -destroy -var-file=environments/dev.tfvars -var "package_path=../dist/soccer.zip" -out=soccer.dev.tfplan` (after `task lambda`), review, then `task infra:apply`; put `prod` in place of each `dev` and add `ENV=prod` for production. Remove that environment's deploy job and its `infra/environments` files in the same change, or the next `Deploy` run creates the deployment again. The site stops answering `/soccer` at once; its own resources are untouched.

## Cost

Lambda stays well inside the free tier at this use. The two alarms are about $0.20 a month per deployment, and the `LeagueUnavailable` custom metric up to $0.30 a month once the account's free custom metrics are used. Logs are a few megabytes a month. IAM roles and policies cost nothing, and the Actions minutes are free for a public repository. About $1 a month at most for both deployments.

## Evidence limits

`task ci` runs the Go tests and the OpenTofu tests against a mocked provider: names, the IAM-only URL, the two permissions, the logs-only policy, the environment, the alarms, the input checks, and the deploy roles' trust and policy. None of it calls AWS, so whether the deploy role's policy covers every call an apply makes is proven only by the first `Deploy` run of each environment; a missing permission fails that run's plan or apply with `AccessDenied`, naming the action. A plan that fails changes nothing; an apply that fails part-way keeps what it changed before the error, and the next run after the fix finishes the rest. The alarm limit rests on AWS's description of `cloudwatch:AlarmActions`, the actions an alarm may take when its state changes; that it also covers an alarm's OK actions has not been tested. The smoke test proves the site reaches the backend and that unsigned calls are refused, not that the league answers. The league alarm has not fired until the league fails; `aws cloudwatch set-alarm-state --alarm-name soccer-dev-server-league-unavailable --state-value ALARM --state-reason test --region us-west-2` proves the email path, and the alarm returns to OK by itself.
