{
  description = "remote-dev pre-built binaries";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      version = "0.2.4";
      binaries = {
        x86_64-linux = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-x86_64-linux.tar.gz";
          hash = "sha256-l1E//cvm91GgSs6cuWY6qzEM8NqatfjGhYpdoCEMS/w=";
        };
        aarch64-linux = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-aarch64-linux.tar.gz";
          hash = "sha256-8NtczMQGda//QTwdZrhu7z7ngnQVxvXP7jMWQT1A+fk=";
        };
        x86_64-darwin = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-x86_64-darwin.tar.gz";
          hash = "sha256-NQ/x4+HjOa3UF6qf9fAy12tw75gc/4K1egoHUeTtGJ4=";
        };
        aarch64-darwin = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-aarch64-darwin.tar.gz";
          hash = "sha256-nXlezuuNxGZvJ2AkKv7NuC7tcZOKysAXtAEFB5jGqu0=";
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
