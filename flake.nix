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
          hash = "sha256-J6Sv+cYKWSNuX5t0tu4S64OcxPp5tztP8nEYYt1V5r8=";
        };
        aarch64-linux = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-aarch64-linux.tar.gz";
          hash = "sha256-e13O6FV+18NnxnRWuPAG/TSLFQsSonQypft4ScBkglo=";
        };
        x86_64-darwin = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-x86_64-darwin.tar.gz";
          hash = "sha256-lEh/T4hKhvIPnJhrWuAVY5YMNc9KG3yXKnEryXA2lgk=";
        };
        aarch64-darwin = {
          url = "https://github.com/M-Adoo/remote-dev-bin/releases/download/v${version}/remote-dev-aarch64-darwin.tar.gz";
          hash = "sha256-R7avyIEdsjJgthO5XMnx4PnhReAsnJB0F78rDPqQDvM=";
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
