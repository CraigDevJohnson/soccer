# Bruno collector execution contract

The October collector used Python HTTP while Bruno ran separate checks. The user required Bruno to execute collection requests. The November continuation replaces that transport and retains the existing Python queue, database, model validation, evidence and report code.

## Boundary

Python chooses one queued day, discovered division or enabled upcoming catalog, persists the attempt and pacing boundary, then invokes the pinned Bruno CLI. Bruno executes one unauthenticated GET and returns a single controlled result. Python accepts the result only after checking the protocol, status, body size and encoding. It validates source identity and facility before committing evidence and normalized records.

Bruno does not decide which division IDs to visit or write archive checkpoints. Python does not execute API requests. This keeps recovery and server backoff in the same transaction model as the original collector.

The subprocess receives an isolated request collection and an allowlisted environment. The personal Bruno collection remains a separate workbench. No raw response report or temporary raw-body file is created. Standard output is captured in bounded memory, and subprocess diagnostic output is not forwarded to the operator. Bruno awaits the output stream's write callback so pipe backpressure cannot truncate a large result.

## Exact bytes and pinned internals

Bruno 4.4.0 exposes `res.getDataBuffer()` in its Developer Mode sandbox. The bridge returns those bytes without JSON parsing or serialization. Requests use identity encoding and disable automatic decompression, so `body_sha256` continues to identify the original HTTP entity bytes. `evidence_sha256` identifies canonical sanitized JSON and usually differs. Unsupported compressed JSON remains a visible invalid source response.

`req.req.maxContentLength` enforces the download cap. The `req.onFail` handler reads the underlying response status and `Retry-After` when a transfer exceeds the cap. These internals are tested against the pinned release; they are not a general compatibility promise for future Bruno releases.

The bridge uses Developer Mode only for this trusted collection. It does not execute server-supplied scripts or use `bru.sendRequest` to bypass the CLI request runner. Redirects, cookies and proxies are disabled. A subprocess failure, cancellation, absent result, malformed frame, invalid base64 or oversize body cannot become an empty successful schedule.

The runtime pins four transitive versions in `bruno/package.json`. They address npm advisories in Faker, UUID, CSV parsing and glob matching. Recheck the locked tree and the loopback transport suite before changing those overrides.

## Required verification

`task collector:test` exercises the real Bruno CLI against loopback HTTP fixtures and the existing collector recovery tests. It must demonstrate discovery and resume, exact byte hashes for valid bodies up to the 8 MiB limit, sanitized persistence, both `Retry-After` forms, authentication refusal, no redirected request, wrong identities, invalid JSON, download caps, failed subprocesses and cancellation. Tests must not require personal Bruno environments or make LPS requests.

Installation locks the dependency graph with `package-lock.json` and uses `npm ci --ignore-scripts`. Relevant upstream documentation: [Bruno run options](https://docs.usebruno.com/bru-cli/run/options), [script API](https://docs.usebruno.com/testing/script/javascript-reference), and [npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/). Installed source and executable fixture tests, rather than those pages alone, establish the internal buffer and failure-metadata contract.

## Preserved research

The original collector is Git commit `9c0344df5590029afbad69cc9a2dea488d4eb192`. The earlier research survives at `69fdb858ab286f8250732fca9605597f1ffd66bc:docs/research/`, including `bruno-collector-capabilities.md`. That document's companion-only recommendation predates the user's correction and this bridge. Its historical datasets were not recovered. Any new live validation creates a new dataset.
