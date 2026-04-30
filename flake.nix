{
  description = "remote-dev pre-built binaries";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      version = "0.2.3";
      binaries = {
        x86_64-linux = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-x86_64-linux.tar.gz";
          hash = "sha256-gqCr+r2QzbH6QMkpwG3vsoZ60vQUpVznMg/iNmiMMXo=";
        };
        aarch64-linux = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-aarch64-linux.tar.gz";
          hash = "sha256-HzRAnDqS6N0VrpcS/Pot/QxyqOONZb3iE60gBwAEzm4=";
        };
        x86_64-darwin = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-x86_64-darwin.tar.gz";
          hash = "sha256-11NCiKtlEReou/kwfl2QGtXwRwupirJhId5JABm4NlU=";
        };
        aarch64-darwin = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-aarch64-darwin.tar.gz";
          hash = "sha256-cYpgSd1MdjmxXRL/QFV5MfHNrFEIy9/ISLfo6855ep4=";
        };
      };
    in
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        binInfo = binaries.${system} or (throw "Unsupported system: ${system}");
      in {
        packages.default = pkgs.stdenv.mkDerivation {
          pname = "remote-dev";
          inherit version;
          src = pkgs.fetchurl {
            inherit (binInfo) url hash;
          };
          sourceRoot = ".";
          dontUnpack = true;
          installPhase = ''
            mkdir -p $out/bin
            tar xzf $src -C $out/bin
            chmod +x $out/bin/remote-dev
          '';
        };
      });
}
