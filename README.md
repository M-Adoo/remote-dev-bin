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
- `cloud/host-runtime-closure-<system>.json`
- `cloud/host-service-image.json`
- `nix-cache/`

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

`Cleanup HostService Test Artifacts` rewrites `host-service-test`, keeps only the
latest 5 successful workflow run records, and removes any completed workflow run
record older than 7 days, including failed or cancelled runs. `main` must not be
force-pushed.
