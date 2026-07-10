{
  description = "remote-dev artifact publisher source";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        checks.publisher-self-test = pkgs.runCommand "remote-dev-bin-publisher-self-test"
          { nativeBuildInputs = [ pkgs.python3 ]; }
          ''
            python3 ${self}/scripts/publish.py self-test
            touch $out
          '';

        devShells.default = pkgs.mkShell {
          packages = [ pkgs.git pkgs.nix pkgs.python3 ];
        };
      });
}
