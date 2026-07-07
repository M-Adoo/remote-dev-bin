# remote-dev-bin

This is the public artifact and execution repository for the private
`M-Adoo/remote-dev` project.

It is public so Nix flakes, HostService hosts, and GitHub Actions can fetch
generated artifacts without private repository credentials. It is not a
general-purpose product distribution channel and does not promise stability,
compatibility, or support for external users.

## Published Refs

- `main`: production release artifacts and production Cloud Run deployment
  metadata.
- `host-service-test`: floating test artifacts and test Cloud Run deployment
  metadata. This branch may be force-pushed by retention cleanup.

Both refs use the same generated artifact schema:

- `build-manifest.json`
- `artifacts/*.tar.gz`
- `artifacts/*.tar.gz.sha256`
- `artifacts/*.build.json`
- `flake.nix`
- `flake.lock`
- `host-runtime-specs/<arch>.json`
- `cloud/agent-runtime-closure-<system>.json`
- `cloud/host-groups-catalog-<system>.json`
- `cloud/host-groups/<group>-<system>.json`
- `cloud/host-service-image.json`
- `nix-cache/`

Host group catalogs use schema v3. Each host group is a published package
bundle with a realized store path, closure manifest, command-relative paths,
and optional contract env snapshots. The catalog includes the
`remote-dev-default-shell-v2` contract for the default workspace shell baseline.
That baseline only promises bash/coreutils; C toolchains live in explicit host
groups and are not part of the empty workspace shell. Host groups are not
project `devShells`.

The flake consumes repository-local tarballs from `artifacts/`; it does not
fetch GitHub Release assets.

## Workflows

`Publish Test Artifacts` is manual and always publishes to
`host-service-test` for the `remote-dev-host-test` project. The source ref is
required so test publishes use the commit or private temporary branch under
test, not an implicit `remote-dev/main`. It defaults to `aarch64` and can build
`x86_64`, `aarch64`, or both.

`Publish Release Artifacts` is manual and always publishes to `main` for the
`remote-dev-host-prod` project. It builds the full binary matrix, deploys prod
Cloud Run from the image digest, and requires the protected `prod` environment
plus `REMOTE_DEV_CONFIRM_PROD=remote-dev-host-prod`.

`Delete Successful Test Run Record` removes successful `Publish Test Artifacts`
and `Cleanup HostService Test Artifacts` workflow run records after those runs
complete. Failed, cancelled, or timed-out test runs are kept for short-term
diagnosis and removed by the scheduled cleanup after 7 days.

`Cleanup HostService Test Artifacts` rewrites `host-service-test`, removes
completed test workflow run records older than 7 days, and removes
`environment=test` GitHub deployments older than 7 days. Test Cloud Run revision
cleanup keeps the latest 20 revisions while also protecting latest ready, latest
created, traffic, and tagged revisions. `main` must not be force-pushed.
