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
cache, agent runtime closure, and host-group data. HostService firstboot fetches
only this archive and its checksum; `build-manifest.json` is audit metadata and
is not a firstboot input.

## Workflows

`Publish Test Artifacts` is manual, requires an explicit private source ref,
defaults to `aarch64`, and publishes only to `host-service-test` and
`remote-dev-host-test`.

`Publish Release Artifacts` is manual, publishes only to
`host-service-release` and `remote-dev-host-prod`, builds the full matrix, and
requires the protected `prod` environment plus
`REMOTE_DEV_CONFIRM_PROD=remote-dev-host-prod`.

Build jobs have private source read authority only. The publish job receives
build outputs and bootstrap resources but no private repository token. Cloud
authority, signing authority, artifact-branch write authority, and deployment
remain in the publish job.
