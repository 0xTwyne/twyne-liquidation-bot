# shell.nix
let
  pkgs = import (builtins.fetchTarball {
    url = "https://github.com/NixOS/nixpkgs/tarball/nixos-unstable";
    sha256 = "sha256:1jkbwvljz9b05zjnxwgj8aadri3sd4wzamp620ywd7lp5s869dxl"; # fill after first run
  }) { config.allowUnfree = true; };
in
pkgs.mkShell {
  buildInputs = [
    pkgs.foundry
    pkgs.act
    pkgs.lcov
    pkgs.python3
    pkgs.poetry
  ];
}
