# Deploying the backend

The Schedule Downloader page (`/soccer` in [the-lobby](https://github.com/CraigDevJohnson/the-lobby)) calls this backend through a Lambda Function URL that accepts only the site's own role, with requests the site signs (the-lobby ADR 0006). `infra/` is the OpenTofu for one such deployment. It is named for the site environment it serves, `soccer-<site_environment>-server`, because the site's permissions boundary lets its role call only backends named `*-<environment>-*`. Nothing here is public: there is no API Gateway, no custom domain and no unsigned path in.

## What it creates

| Resource | Name | Why |
| --- | --- | --- |
| Lambda function | `soccer-dev-server` | The Go server from `task lambda`, `provided.al2023` on arm64, 256 MB, 10-second timeout, with `LINK_BASE_URL` and the optional `LEAGUE_*` settings from the README |
| Function URL | on the function | `AWS_IAM` auth; the site's `SOCCER_BACKEND_URL` |
| Two function permissions | `AllowSiteThroughFunctionUrl`, `AllowSiteInvokeThroughFunctionUrl` | `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` for the site's role alone, the second only through the URL |
| IAM role and inline policy | `soccer-dev-server`, `logs` | Lambda's execution role; it may write its own log group and nothing else |
| Log group | `/aws/lambda/soccer-dev-server` | The code's JSON log lines, kept 30 days |
| Metric filter and alarm | `soccer-dev-server-league-unavailable` | Counts the code's `league_unavailable` lines and emails on the first in any five minutes |
| Alarm | `soccer-dev-server-failing` | Lambda errors: a crash or a run that hit the timeout |

The site's role is the only principal the function grants a call to. AWS also lets a principal in the same account call the URL when its own identity policy allows `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`; the account's workload roles carry neither (aws-design G7), and the administrator profile does, which step 6 uses. Unsigned requests get a 403.

Both alarms notify the workloads account's existing `alerts` topic in us-west-2, which already lets CloudWatch publish and emails Craig (aws-setup `aws-design.md`, section 7). There is no new topic and no subscription to confirm. Every resource carries `project = soccer` and the tags in `tags` (aws-design G10).

## Before you start

On the Mac: OpenTofu 1.13 or newer, [Task](https://taskfile.dev), Go (the version in `go.mod`), `zip`, and the AWS CLI with the `workloads-admin` profile from aws-setup. The site's dev environment must already be deployed, because the Function URL is granted to its role, `site-dev-server`. `git pull` on `main` first.

`task ci` runs everything the pull-request checks run, including the offline OpenTofu tests, and needs no credentials.

## Deploy

Everything below runs in `/Users/craigjohnson/Projects/soccer`. Only step 5 changes AWS, and it runs only after you have approved that plan (aws-design G11). Check each output against what is shown before going on. The shell variables carry over from step to step, so use one terminal.

1. **Sign in and read the account.**

   ```sh
   export AWS_PROFILE=workloads-admin
   aws sso login
   account=$(aws sts get-caller-identity --query Account --output text); echo "$account"
   ```

   `aws sso login` opens the browser for the Identity Center approval and prints `Successfully logged into Start URL: …`. The last line prints the 12-digit workloads account id.

2. **Point the root at its state.** The state lives in the bucket the site's own roots use (the-lobby `infra/README.md`), under its own key, `soccer/dev/terraform.tfstate`, with the bucket's native lock file.

   ```sh
   bucket=$(aws s3api list-buckets --query "Buckets[?starts_with(Name,'craigdevjohnson-tofu-state')].Name" --output text); echo "$bucket"
   sed "s/example-replace-with-the-private-state-bucket/$bucket/" infra/backend.hcl.example > infra/backend.hcl
   task infra:init
   ```

   The first line prints exactly one bucket name; if it prints nothing or two names, stop. `task infra:init` prints `Successfully configured the backend "s3"!`, the provider install, and ends with `OpenTofu has been successfully initialized!`. `infra/backend.hcl` is gitignored.

3. **Write the private inputs.** Read the site's role and the alerts topic:

   ```sh
   site_role=$(aws iam list-roles --query "Roles[?RoleName=='site-dev-server'].Arn" --output text); echo "$site_role"
   topic=$(aws sns list-topics --region us-west-2 --query "Topics[?contains(TopicArn,'alerts')].TopicArn" --output text); echo "$topic"
   ```

   The first prints `arn:aws:iam::<account>:role/site-dev-server`, the second `arn:aws:sns:us-west-2:<account>:alerts`, one line each. Then write the file (gitignored) and read it back:

   ```sh
   cat > infra/deployment.tfvars <<EOF
   account_id       = "$account"
   site_environment = "dev"
   site_role_arn    = "$site_role"
   link_base_url    = "https://dev.craigdevjohnson.com/soccer/link/"
   alert_topic_arn  = "$topic"
   tags             = { Site = "craigdevjohnson.com" }
   EOF
   cat infra/deployment.tfvars
   ```

   Every value must be filled in; an empty `""` means a lookup above printed nothing. `link_base_url` is where the dev site serves Session links, so the backend's `POST /links` answers with addresses on the site. `Site` is the-lobby's cost tag, so the site's $5 budget covers this backend. The league settings stay at the code's defaults (Boise); `infra/deployment.tfvars.example` shows how to set them.

4. **Plan.**

   ```sh
   task infra:plan
   ```

   This builds `dist/soccer.zip` (it prints the zip's `ls -l` line), then plans. Expect creates only, ending in `Plan: 10 to add, 0 to change, 0 to destroy.`: `aws_cloudwatch_log_group.server`, `aws_iam_role.server`, `aws_iam_role_policy.server`, `aws_lambda_function.server`, `aws_lambda_function_url.server`, `aws_lambda_permission.site_url`, `aws_lambda_permission.site_invoke`, `aws_cloudwatch_log_metric_filter.league_unavailable`, `aws_cloudwatch_metric_alarm.league_unavailable` and `aws_cloudwatch_metric_alarm.failing`. Any change or destroy, or a resource not on that list, is a stop condition. The plan is saved as `infra/soccer.tfplan` (gitignored), and that exact plan is what the next step applies.

5. **Apply** the plan you reviewed and approved.

   ```sh
   task infra:apply
   ```

   This prints a `Creating...` and `Creation complete` line per resource and ends with `Apply complete! Resources: 10 added, 0 changed, 0 destroyed.` followed by the outputs: `function_arn`, `function_name`, `function_url` and `log_group_name`.

6. **Check it from the Mac.**

   ```sh
   url=$(tofu -chdir=infra output -raw function_url); echo "$url"
   curl -sS -o /dev/null -w '%{http_code}\n' "${url%/}/healthz"
   ```

   This prints the address, `https://<id>.lambda-url.us-west-2.on.aws/`, then `403`: an unsigned call is refused. Your administrator role may call the address, so sign one:

   ```sh
   eval "$(aws configure export-credentials --profile workloads-admin --format env)"
   curl -sS -o /dev/null -w '%{http_code}\n' --aws-sigv4 "aws:amz:us-west-2:lambda" \
     --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" -H "x-amz-security-token: $AWS_SESSION_TOKEN" \
     "${url%/}/healthz"
   curl -sS --aws-sigv4 "aws:amz:us-west-2:lambda" \
     --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" -H "x-amz-security-token: $AWS_SESSION_TOKEN" \
     "${url%/}/divisions" | head -c 200; echo
   ```

   This prints `204`, then the start of `{"divisions":[{"id":"…","name":"…"`: the function reached the league and read this week's divisions at Boise. The exported credentials expire with the SSO session.

7. **Connect the site** in the-lobby: give its function `SOCCER_BACKEND_URL` set to `$url` and `SOCCER_BACKEND_SIGNING=lambda`, and let `site-dev-server` call `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` on `tofu -chdir=infra output -raw function_arn` (its boundary already allows `*-dev-*`). Until then `/soccer` says the backend is not answering.

## Updating the code

After a merge to `main`: `git pull`, `task infra:plan` (expect `Plan: 0 to add, 1 to change, 0 to destroy.`, the function's code), review, then `task infra:apply` once approved. The same source and Go toolchain give the same zip (the build strips paths and commit stamps, and the archive carries a fixed time and mode), so a merge that changes no Go code plans no change.

## Removing it

`tofu -chdir=infra plan -destroy -var-file=deployment.tfvars -var "package_path=$PWD/dist/soccer.zip" -out=soccer.tfplan`, review, then `task infra:apply` once approved. The site stops answering `/soccer` at once; its own resources are untouched.

## Cost

Lambda stays well inside the free tier at this use. The two alarms are about $0.20 a month, and the `LeagueUnavailable` custom metric up to $0.30 a month once the account's free custom metrics are used. Logs are a few megabytes a month. About $0.50 a month at most.

## Evidence limits

`task ci` runs the Go tests and the OpenTofu tests against a mocked provider: names, the IAM-only URL, the two permissions, the logs-only policy, the environment, the alarms and the input checks. None of it calls AWS. Before relying on it, a live check must show the site's signed call reaching the backend (step 7 and a `/soccer` search on the dev site), a Session link fetched through the site, and one alarm email. The league alarm has not fired until the league fails; `aws cloudwatch set-alarm-state --alarm-name soccer-dev-server-league-unavailable --state-value ALARM --state-reason test --region us-west-2` proves the email path, and the alarm returns to OK by itself.
