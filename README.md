# remote-dev-bin

Pre-built binaries for [remote-dev](https://github.com/M-Adoo/remote-dev).

## Installation

### Via Nix (Recommended)

```bash
nix profile install github:M-Adoo/remote-dev-bin
```

Or in a flake input:

```nix
{
  inputs.remote-dev-bin.url = "github:M-Adoo/remote-dev-bin";

  outputs = { remote-dev-bin, ... }: {
    # Use remote-dev-bin.packages.<system>.default
  };
}
```

### Direct Download

```bash
# Detect architecture and download
ARCH=$(uname -m)
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
VERSION="0.1.0"

curl -L "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${VERSION}/remote-dev-${ARCH}-${OS}.tar.gz" | tar xz
chmod +x remote-dev
sudo mv remote-dev /usr/local/bin/
```

## Supported Platforms

| Platform | Architecture |
|----------|-------------|
| Linux | x86_64, aarch64 |
| macOS | x86_64 (Intel), aarch64 (Apple Silicon) |

## Version Pinning

Each release is tagged with a version matching the `remote-dev` source.
Pin to a specific version in your flake:

```nix
inputs.remote-dev-bin.url = "github:M-Adoo/remote-dev-bin/v0.1.0";
```

## How It Works

This repository is automatically updated by CI in the private `remote-dev`
source repository. On each release:

1. CI cross-compiles `remote-dev` for all supported platforms
2. Uploads binaries as GitHub Release assets here
3. Updates `flake.nix` with correct hashes

The `flake.nix` uses `fetchurl` to wrap pre-built binaries as Nix packages,
so `nix build` only downloads—no compilation needed.
