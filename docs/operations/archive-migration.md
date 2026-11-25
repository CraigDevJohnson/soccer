# Move the Boise archive and run its refresh jobs

SQLite remains the sporting record. A generation contains a database, its referenced sanitized evidence, two coverage reports and a manifest. A separate Arm Lambda runs the collector with private S3 storage in `us-west-2`. The calendar Lambda and its role have no archive permissions.

## Freeze and restore a local generation

The November 25 rehearsal used installed release `bc6681aedf0cbac9e98bd377a706f2630ea05753`. Its last collection was November 11. It contained 93,250 games, 23,174 registrations and 10,005 snapshots backed by 10,004 distinct evidence files. The 23 invalid dates and two failed divisions remain in the ledger. The archive covers 6,136 verified dates out of 6,159 requested. The source's all-time coverage is unknown.

Use an owner-private directory outside Git. Set `release` to the full commit of the collector that produced the source. The export takes the collector lock, backs up SQLite, checks all tables, rebuilds the reports for comparison and packages only the referenced files. It refuses a busy writer. A new private restore verifies the evidence, identity, row hashes, integrity and foreign keys.

```sh
install_dir="$HOME/Library/Application Support/LPS Boise Collector"
release=$(basename "$(readlink "$install_dir/current")")
task archive:export -- --data-dir "$install_dir/data/boise-history" --output "$install_dir/generations" --release "$release"
task archive:verify -- --manifest "$install_dir/generations/$generation/manifest.json"
task archive:restore -- --manifest "$install_dir/generations/$generation/manifest.json" --output "$restore_dir"
```

Set `generation` to the directory name printed by export and `restore_dir` to a new directory. Keep the original and an independent verified local copy. The rehearsal bundle was 39,553,015 bytes compressed. Its SHA-256 was `28ccf021480acc51a7cc44260125a916206cdda536fe48b25600c3ee39a4db9d`. The 17 restored tables matched all source rows including their identifiers. A later collection produces a different generation. Re-export the final frozen dataset at cutover.

## Bootstrap dev on an approved plan

1. Land reviewed source through the repository's normal process. Configure the GitHub environments `archive-image-dev`, `archive-import-dev`, `archive-release-dev`, `archive-run-dev` and `archive-activation-dev` to accept only `main`. Require Craig's approval for import, collection and activation. Configure the corresponding production environments and the read-only `archive-recovery-prod` environment with the production reviewer before creating their roles. Require production approval for image publication and release as well. The OIDC trust includes the repository and owner IDs. A branch rule on each environment is part of that trust.
2. Use the archive root and its own state key. With the workloads profile selected, run `task archive:infra:init ENV=dev` and `task archive:infra:plan ENV=dev`. Review resource names, role trust, S3 policy, image repository and plan totals. The initial committed inputs create the storage and delivery roles. Craig must approve this exact plan before `task archive:infra:apply ENV=dev`. A cloud thread may run these approved infrastructure steps. See the [deployment rules](deploy.md).
3. Dispatch **Soccer archive**, operation `image`, target `dev`, on the reviewed `main` revision. The job publishes a single-platform Arm image tagged with the source SHA. Inspect the image scan and record its digest. Set that digest as `image_digest` in the environment's reviewed archive tfvars. Keep it there on subsequent plans. Returning it to null proposes worker removal. Review and approve the first worker/role plan before applying. A new deployment starts with collection off and a disabled schedule.
4. For the **one-time Mac transfer**, obtain Craig's approval of the generation, destination, short-lived importer and exception to the owning AWS foundation's manual data-write rule (G3). Record that exception in the foundation repository and this runbook before running the upload. Include `staging_access = { principal_arn = "<exact Identity Center role ARN>", generation = "<64 hex>" }` in the reviewed archive role plan. First bootstrap and staging access can share an approved plan. Remove the temporary trust through a reviewed role change after verification.
5. On the Mac, assume the resulting environment-specific `stage` role through a short-lived Identity Center profile. Set `AWS_PROFILE` to that role profile. Run `task archive:stage -- --environment dev --account "$workloads_account" --manifest "$manifest"`. The role can read and conditionally create the two approved files in that generation's staging prefix. The command checks checksums and reads both objects back.
6. Dispatch **Soccer archive**, operation `import`, target `dev`, with the generation ID. It downloads both staged files, restores and verifies the complete archive, creates the immutable generation files and reads them back. It creates `control/active.json` only after verification. Repeating that first import is safe. A different active pointer stops the import. Dispatch `verify` to restore the generation selected by the active pointer and report the result without publishing any archive contents.
7. For later code releases, finish any active job and close the collection gate before using the `image` and `release` operations. Supply the immutable image digest to `release`. The production image operation copies the digest currently running in dev into the production registry. The production release checks it against dev again. Each environment owns its own bucket, checkpoints, worker and roles. Review the production infrastructure and baseline separately.

AWS application data never enters Git, a workflow artifact or a browser. A restore runs in private temporary storage. S3 blocks public access, rejects insecure transport, requires conditional writes, uses SSE-S3 and retains object versions. Job, import and image roles have separate permissions. A job role cannot change collection settings, delete evidence or invoke the calendar backend. Access to the buckets and workflow environments is administrative.

## Run one bounded refresh

Coordinate the sole live writer before any job. Stop local collection, check that the latest job is terminal and record any provider backoff. Disable every local launch path, export under the exporter's own lock, then keep the Mac writer disabled through authority transfer. An independently held exclusive lock makes export refuse. If the handoff is interrupted, keep collection off until an operator chooses one authoritative copy.

Dispatch **Soccer archive activation** on `main`. Mode `off` closes the request gate. Mode `manual` allows explicit jobs. Mode `daily` is production-only. Shutting off collection prevents further requests at the next gate check. Allow an already-started request to finish. Before enabling a writer, activation checks that the other environment is off, its job is idle or finished, and its provider backoff has elapsed. An unreadable peer blocks the switch. Keep the Mac writer stopped independently.

Dispatch **Soccer archive**, operation `run`, with a JSON job. Each job has one end date, up to 28 inclusive dates, the upcoming catalog and discovered divisions. The first budget is 1 to 100 total upstream attempts. The worker reserves each attempt before HTTP, including retries. It retains sanitized outcomes and provider backoff before issuing another request.

| Job input | Result |
| --- | --- |
| `{"operation":"status"}` | Read safe checkpoint status without contacting the league |
| `{"operation":"refresh","through":"2026-11-25","days":1,"budget":3}` | Submit a dated job or continue that same job |
| `{"operation":"resume"}` | Continue the unfinished job with its remaining budget |
| `{"operation":"resume","job":"refresh-2026-11-25","request_id":"extension-1","additional_budget":5}` | Extend an exhausted job once for that ID |
| `{"operation":"resume","job":"refresh-2026-11-25","retry_blocked_attempt":3}` | After diagnosis, retry that specific blocked attempt, subject to remaining budget and backoff |

`completed_with_gaps` retains unavailable records. A job paused for budget requires an explicit extension. An interrupted attempt still counts. A provider authentication refusal stops work for operator review. Requests use the fixed public Boise endpoints and the committed Bruno transport. Only one invocation runs at a time. On retry it restores the base generation and replays the job's S3 journal in order. Publication verifies stored bytes before conditionally advancing the active pointer. A conflicting pointer requires reconciliation.

Approve a small manual trial first. Check the resulting generation, added observations, retained gaps, request count, elapsed time and Lambda's reported memory. Restore the published result through `verify`. When changing authority between environments, finish the old job, close its gate and honor any remaining backoff. Import the final verified baseline in production. After the bounded production trial and cost review, approve `daily`. It runs at 04:00 America/Boise for the previous 13 dates and the current date, plus the upcoming catalog, with a 100-attempt budget. Check the first actual execution through a verified publication or a recorded pause.

## Verify the independent production copy

The first production plan configures S3 replication to `soccer-archive-prod-recovery-<account>` in Oregon. Apply and verify the replication configuration before the first production import. S3 copies new versions under `generations/`, `journal/` and `control/`. It excludes staging data and operating settings. The recovery bucket blocks public access, retains versions and accepts replica writes from the S3 replication role. The archive worker and import role cannot change recovery data. Same-region storage protects this archive against deletion and corruption. Regional disaster recovery is outside the account's stated backup objective.

After a verified publication, dispatch **Soccer archive**, operation `verify-recovery`, target `prod`. Replication is asynchronous. Wait for completion and retry a pending verification. Check that the independently restored generation matches the selected primary generation. When recovering an unfinished job, also reconcile journal and checkpoint versions through its last charged attempt. A successful baseline restore alone cannot prove that newer in-flight work was replicated. S3 live replication starts with new eligible writes. Any older objects require a separately approved reconciliation. [AWS S3 replication](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)

## Recover after an interruption

For an interrupted invocation, query status and inspect the job's charged attempts and next eligible time. Use `resume` after that time. A lost response remains an interrupted attempt. Check the provider's status before authorizing another request. Known invalid dates and missing scores stay visible.

For a suspected bad generation, turn collection off. Keep the active pointer, worker checkpoint, settings, immutable journal and generation versions. Restore the selected generation in an independent directory or a separate approved recovery deployment. Compare identities, evidence, report hashes and accepted attempts after that generation. The import role cannot overwrite the active pointer. A live rollback or deliberate writer change needs a reviewed recovery plan that reconciles later accepted observations and the job checkpoint together. Keep the Mac recovery copy until a hosted restore has been demonstrated.

## Estimate the bill before enabling the schedule

In Oregon, 2 GiB of Arm Lambda for ten minutes a day for 30 days costs about **$0.48/month** before credits. One compressed 39.6 MB baseline costs under **$0.001/month** in S3 Standard. Retaining one new bundle of that size daily adds about 1.1 GiB each month, increasing the monthly storage rate by about **$0.03** per month of accumulated history. Actual growth depends on new evidence. Immutable generations and journals have no automatic expiry. Superseded control versions expire after 30 days. [Oregon Lambda rates](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSLambda/current/us-west-2/index.json) · [Oregon S3 rates](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-west-2/index.json)

At list rates, four standard production alarms add about $0.40/month. One active custom attention metric can add $0.30/month. With 1 GiB of image storage at $0.10/month, those items plus the example compute total **$1.28/month** before S3, logs, dev, growth and tax. The attention alarm covers budget pauses, blocked requests and uncertain interruptions. The other alarms cover runtime errors and dropped delivery. Check the active generation to establish freshness. Production also pays for replica storage and replica PUTs. Replicating one 39.6 MB generation per day adds another $0.03 per month of accumulated storage at this size. Same-region replication adds no cross-region transfer charge. The protected workflows start no always-on compute. The organization budget sends alerts and does not cap the bill. A network-isolated Arm container limited to 2 GiB and 1.15 CPUs ran a two-request fixture job against a disposable copy of the real baseline in 76.7 seconds. The new version preserved all 93,250 games and restored with two additional snapshots. Only an approved dev run can establish Lambda duration, peak memory, effective IAM permissions and the actual bill. [ECR pricing](https://aws.amazon.com/ecr/pricing/) · [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)

The private Lobby query workspace and visitor displays remain later projects. This migration provides the dataset and jobs that those projects will use.
