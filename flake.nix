{
  description = "remote-dev pre-built binaries";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      version = "0.1.0";
      binaries = {
        x86_64-linux = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-x86_64-linux.tar.gz";
          hash = "";
        };
        aarch64-linux = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-aarch64-linux.tar.gz";
          hash = "";
        };
        x86_64-darwin = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-x86_64-darwin.tar.gz";
          hash = "";
        };
        aarch64-darwin = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-aarch64-darwin.tar.gz";
          hash = "";
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
