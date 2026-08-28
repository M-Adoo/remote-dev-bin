# remote-dev-bin

This is the public publisher and artifact repository for the private
`M-Adoo/remote-dev` project. It is not a compatibility or support surface for
external consumers.

## Branch roles

- `main` contains publisher source and GitHub Actions only. It never contains
  generated release artifacts.
- `host-service-release` contains production artifacts and production Cloud Run
  deployment metadata. It is append-only and must never be force-pushed.
- `host-service-test` contains test artifacts and test Cloud Run deployment
  metadata. Retention cleanup may force-push this branch.

The release and test artifact branches use the same schema. Test may select a
single Linux architecture; release always publishes the complete matrix.

Each artifact commit contains only:

- `build-manifest.json`
- `artifacts/remote-dev-<system>.tar.gz` plus `.sha256` and `.build.json`
- `artifacts/remote-dev-host-<linux-system>.tar.gz` plus `.sha256` and `.build.json`
- `cloud/host-service-image.json`

There are no GitHub Release Assets, root artifact flake, expanded `nix-cache/`,
`host-runtime-specs/`, closure/catalog files, platform child commits, or index
commit.

The CLI archive is itself a platform-specific flake. It contains `flake.nix`,
`flake.lock`, and `bin/remote-dev`, and exposes only its declared system. Install
from an immutable artifact commit with:

```text
tarball+https://raw.githubusercontent.com/M-Adoo/remote-dev-bin/<commit>/artifacts/remote-dev-<system>.tar.gz
```

The host archive is self-contained for one Linux system. It carries its
manifest, bundle-owned firstboot entrypoint and host-control scripts, signed Nix
cache, staging flake-lock SHA-256, agent runtime closure, and host-group data.
HostService firstboot fetches only this archive and its checksum;
`build-manifest.json` is audit metadata and is not a firstboot input.

## Workflows

`Publish Test Artifacts` is manual, requires an explicit private source ref,
defaults to `aarch64`, and publishes only to `host-service-test` and
`remote-dev-host-test`. After readiness it mints an audience-bound Google OIDC
ID Token for the exact test deployer and verifies provider refresh through the
machine-only ops surface. It never requires an Accounts user Access Token.

`Verify HostService Test Deployment` is a protected, non-deploying verification
for the currently serving test revision. It verifies 100% traffic, immutable
image and artifact refs, enabled numeric Secret bundle pins, exact bundle IAM,
`/ready`, and provider refresh. It does not build, publish, create a revision,
or call Accounts-authenticated runtime and Host Admin routes.

`Publish Release Artifacts` is manual, publishes only to
`host-service-release` and `remote-dev-host-prod`, builds the full matrix, and
requires the protected `prod` environment plus
`REMOTE_DEV_CONFIRM_PROD=remote-dev-host-prod`. Its first approved run creates
the artifact-only `host-service-release` branch when absent; later runs append
without force-pushing, and release runs are serialized to avoid initialization
races.

Build jobs have private source read authority only. The publish job receives
build outputs and bootstrap resources but no private repository token. Cloud
authority, signing authority, artifact-branch write authority, and deployment
remain in the publish job.

Before a production deployment, the protected publish job checks that the
Accounts owner and login-token-key Secrets exist, have enabled versions, and
grant `secretAccessor` only as configured to the exact HostService runtime
service account. The publisher reads metadata and IAM policy only; it never
reads Secret payloads or creates production Secrets.

Production Artifact Registry cleanup is defined in
`infra/host-service-artifact-cleanup-policies.json`. It deletes `host-service`
versions older than seven days while retaining at least the ten most recent
versions. Both the dedicated `Sync Production Artifact Cleanup` workflow and
each production release apply the policy with deletion enabled and require an
exact live readback. They run only in the protected `prod` environment with the
fixed production project and deployer identity.

Both HostService deployments allow unauthenticated Cloud Run invocation because
the V7 application owns per-route Accounts JWT and Google OIDC authorization.
Each deployment must pass a public `/ready` check reporting runtime API version
7 before the workflow continues.
The production publisher pins the exact deployer service account, mints Google
OIDC for `https://adoo.dev`, and performs release checks only through
`/v1/ops/provider-spots` and `/v1/ops/provider-spots/refresh`; it does not reuse
Web Host Admin credentials or routes.
