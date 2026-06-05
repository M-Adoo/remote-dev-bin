#!/usr/bin/env python3
"""remote-dev-bin publish helper.

The GitHub workflows in this repository build source artifacts, then use this
script for the fail-closed parts that are easy to unit test locally: target
selection, matrix expansion, build manifest validation, branch tree rendering,
and retention cleanup.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO = "M-Adoo/remote-dev-bin"
SOURCE_REPO = "M-Adoo/remote-dev"
HOST_CONFIG_ID = "remote-dev-nixos-host-v3"
IMAGE_CONTRACT_ID = "remote-dev-cloud-host-v1"
HOST_IMAGE_SPEC_SCHEMA_VERSION = 4
HOST_IMAGE_SCHEMA_VERSION = 3
FIRSTBOOT_SCHEMA_VERSION = 1
AWS_BOOTSTRAP_FLAKE = "cloud/aws-bootstrap-flake.nix"
AWS_BOOTSTRAP_LOCK = "cloud/aws-bootstrap-flake.lock"
AWS_BOOTSTRAP_TOPLEVEL_ATTR = (
    "nixosConfigurations.remote-dev-host.config.system.build.toplevel"
)
NIX_CACHE_DIR = "nix-cache"
ARTIFACT_DIR = "artifacts"
HOST_IMAGE_SPEC_DIR = "host-image-specs"
DEFAULT_NIXPKGS_REV = "0000000000000000000000000000000000000000"
GITHUB_MAX_BLOB_BYTES = 100_000_000


@dataclass(frozen=True)
class SystemTarget:
    system: str
    arch: str
    cargo_target: str
    os: str

    @property
    def remote_dev_artifact(self) -> str:
        return f"remote-dev-{self.system}"

    @property
    def hostctrl_artifact(self) -> str:
        return f"remote-dev-hostctrl-{self.system}"


SYSTEMS: dict[str, SystemTarget] = {
    "x86_64-linux": SystemTarget(
        "x86_64-linux", "x86_64", "x86_64-unknown-linux-gnu", "ubuntu-latest"
    ),
    "aarch64-linux": SystemTarget(
        "aarch64-linux", "aarch64", "aarch64-unknown-linux-gnu", "ubuntu-24.04-arm"
    ),
    "x86_64-darwin": SystemTarget(
        "x86_64-darwin", "x86_64", "x86_64-apple-darwin", "macos-14"
    ),
    "aarch64-darwin": SystemTarget(
        "aarch64-darwin", "aarch64", "aarch64-apple-darwin", "macos-14"
    ),
}


@dataclass(frozen=True)
class TargetConfig:
    name: str
    branch: str
    cloud: str
    project: str
    remote_dev_systems: tuple[str, ...]
    host_arches: tuple[str, ...]
    retention_commits: int | None
    retention_days: int | None
    protected_environment: str | None = None
    confirm_prod: str | None = None


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def target_config(name: str, arch_selection: str = "both") -> TargetConfig:
    if name == "release":
        return TargetConfig(
            name="release",
            branch="main",
            cloud="prod",
            project="remote-dev-host-prod",
            remote_dev_systems=(
                "x86_64-linux",
                "aarch64-linux",
                "x86_64-darwin",
                "aarch64-darwin",
            ),
            host_arches=("x86_64", "aarch64"),
            retention_commits=None,
            retention_days=None,
            protected_environment="prod",
            confirm_prod="remote-dev-host-prod",
        )
    if name != "host-service-test":
        raise Fail(f"unsupported target {name!r}")
    if arch_selection == "both":
        arches = ("x86_64", "aarch64")
    elif arch_selection in ("x86_64", "aarch64"):
        arches = (arch_selection,)
    else:
        raise Fail("--arch must be x86_64, aarch64, or both")
    systems = tuple(f"{arch}-linux" for arch in arches)
    return TargetConfig(
        name="host-service-test",
        branch="host-service-test",
        cloud="test",
        project="remote-dev-host-test",
        remote_dev_systems=systems,
        host_arches=arches,
        retention_commits=5,
        retention_days=7,
    )


class Fail(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def json_load(path: Path) -> Any:
    return json.loads(path.read_text())


def run(args: list[str], cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode != 0:
        detail = ""
        if capture:
            detail = f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise Fail(f"{' '.join(args)} failed with exit code {result.returncode}{detail}")
    return result.stdout if capture else ""


def run_with_input(args: list[str], text: str, cwd: Path | None = None) -> None:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        input=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise Fail(
            f"{' '.join(args)} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def require_clean_source(source_dir: Path) -> None:
    status = run(["git", "status", "--porcelain"], cwd=source_dir, capture=True)
    if status.strip():
        raise Fail("remote-dev source checkout is dirty; publish requires a clean source tree")


def build_matrix(config: TargetConfig) -> dict[str, Any]:
    include: list[dict[str, str]] = []
    for system in config.remote_dev_systems:
        target = SYSTEMS[system]
        include.append(
            {
                "kind": "remote-dev",
                "package": "remote-dev",
                "binary": "remote-dev",
                "system": target.system,
                "arch": target.arch,
                "cargo_target": target.cargo_target,
                "os": target.os,
                "artifact": target.remote_dev_artifact,
            }
        )
    for arch in config.host_arches:
        target = SYSTEMS[f"{arch}-linux"]
        include.append(
            {
                "kind": "hostctrl",
                "package": "remote-dev-hostctrl",
                "binary": "remote-dev-hostctrl",
                "system": target.system,
                "arch": target.arch,
                "cargo_target": target.cargo_target,
                "os": target.os,
                "artifact": target.hostctrl_artifact,
            }
        )
    return {"include": include}


def cmd_matrix(args: argparse.Namespace) -> None:
    print(json.dumps(build_matrix(target_config(args.target, args.arch)), sort_keys=True))


def cmd_write_binary_manifest(args: argparse.Namespace) -> None:
    tarball = Path(args.tarball)
    if not tarball.is_file():
        raise Fail(f"missing tarball {tarball}")
    digest = sha256_file(tarball)
    sha_file = Path(f"{tarball}.sha256")
    sha_file.write_text(f"{digest}  {tarball.name}\n")
    manifest = {
        "schema_version": 1,
        "kind": args.kind,
        "target": args.target,
        "source_repo": SOURCE_REPO,
        "source_ref": args.source_ref,
        "source_sha": args.source_sha,
        "system": args.system,
        "arch": args.arch,
        "cargo_target": args.cargo_target,
        "package": args.package,
        "binary": args.binary,
        "artifact": args.artifact,
        "tarball": tarball.name,
        "sha256": digest,
        "generated_at": utc_now(),
    }
    json_dump(Path(args.output), manifest)


def cmd_write_image_manifest(args: argparse.Namespace) -> None:
    tarball = Path(args.tarball)
    if not tarball.is_file():
        raise Fail(f"missing image tarball {tarball}")
    manifest = {
        "schema_version": 1,
        "kind": "host-service-image",
        "target": args.target,
        "source_repo": SOURCE_REPO,
        "source_ref": args.source_ref,
        "source_sha": args.source_sha,
        "platform": "linux/amd64",
        "tarball": tarball.name,
        "sha256": sha256_file(tarball),
        "generated_at": utc_now(),
    }
    json_dump(Path(args.output), manifest)


def collect_build_manifests(artifacts_dir: Path) -> list[dict[str, Any]]:
    manifests = []
    for path in sorted(artifacts_dir.rglob("*.build.json")):
        value = json_load(path)
        value["_manifest_path"] = str(path)
        manifests.append(value)
    if not manifests:
        raise Fail(f"no *.build.json manifests found under {artifacts_dir}")
    return manifests


def verify_binary_manifest(manifest: dict[str, Any], artifacts_dir: Path, config: TargetConfig) -> None:
    required = [
        "schema_version",
        "kind",
        "target",
        "source_sha",
        "system",
        "arch",
        "artifact",
        "tarball",
        "sha256",
    ]
    for field in required:
        if not manifest.get(field):
            raise Fail(f"{manifest.get('_manifest_path')}: missing {field}")
    if manifest["schema_version"] != 1:
        raise Fail(f"{manifest['_manifest_path']}: unsupported schema_version")
    if manifest["target"] != config.name:
        raise Fail(f"{manifest['_manifest_path']}: target mismatch")
    tarball = artifacts_dir / manifest["tarball"]
    if not tarball.is_file():
        raise Fail(f"{manifest['_manifest_path']}: tarball {tarball.name} is missing")
    actual = sha256_file(tarball)
    if actual != manifest["sha256"]:
        raise Fail(f"{tarball.name}: sha256 mismatch, manifest has {manifest['sha256']}, got {actual}")
    sha_file = artifacts_dir / f"{manifest['tarball']}.sha256"
    if not sha_file.is_file():
        raise Fail(f"{manifest['_manifest_path']}: missing {sha_file.name}")
    if manifest["sha256"] not in sha_file.read_text():
        raise Fail(f"{sha_file.name}: does not contain manifest sha256")


def expected_artifacts(config: TargetConfig) -> set[str]:
    names = {SYSTEMS[system].remote_dev_artifact for system in config.remote_dev_systems}
    names.update(SYSTEMS[f"{arch}-linux"].hostctrl_artifact for arch in config.host_arches)
    return names


def select_binary_manifests(
    manifests: list[dict[str, Any]], artifacts_dir: Path, config: TargetConfig, source_sha: str
) -> list[dict[str, Any]]:
    binaries = [m for m in manifests if m.get("kind") in ("remote-dev", "hostctrl")]
    for manifest in binaries:
        verify_binary_manifest(manifest, artifacts_dir, config)
        if manifest["source_sha"] != source_sha:
            raise Fail(
                f"{manifest['_manifest_path']}: source_sha {manifest['source_sha']} "
                f"does not match expected {source_sha}"
            )
    got = {m["artifact"] for m in binaries}
    want = expected_artifacts(config)
    missing = sorted(want - got)
    extra = sorted(got - want)
    if missing or extra:
        raise Fail(f"artifact set mismatch: missing={missing}, extra={extra}")
    return sorted(binaries, key=lambda m: m["artifact"])


def remove_generated_paths(branch_dir: Path) -> None:
    for relative in [
        "build-manifest.json",
        ARTIFACT_DIR,
        HOST_IMAGE_SPEC_DIR,
        "host-image-spec.json",
        "host-service-test-metadata.json",
        "cloud-images.json",
        "remote-dev-hostctrl-x86_64-linux.tar.gz",
        "remote-dev-hostctrl-x86_64-linux.tar.gz.sha256",
        "remote-dev-hostctrl-aarch64-linux.tar.gz",
        "remote-dev-hostctrl-aarch64-linux.tar.gz.sha256",
        "remote-dev-x86_64-linux.tar.gz",
        "remote-dev-x86_64-linux.tar.gz.sha256",
        "remote-dev-aarch64-linux.tar.gz",
        "remote-dev-aarch64-linux.tar.gz.sha256",
        "remote-dev-x86_64-darwin.tar.gz",
        "remote-dev-x86_64-darwin.tar.gz.sha256",
        "remote-dev-aarch64-darwin.tar.gz",
        "remote-dev-aarch64-darwin.tar.gz.sha256",
        "cloud/host-service-image.json",
        "cloud/aws-builder-flake.nix",
        "cloud/aws-builder-flake.lock",
        AWS_BOOTSTRAP_FLAKE,
        AWS_BOOTSTRAP_LOCK,
        "cloud/aws-bootstrap-closure-x86_64-linux.json",
        "cloud/aws-bootstrap-closure-aarch64-linux.json",
        NIX_CACHE_DIR,
    ]:
        path = branch_dir / relative
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def copy_artifacts(
    artifacts_dir: Path, branch_dir: Path, manifests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out_dir = branch_dir / ARTIFACT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for manifest in manifests:
        tarball = artifacts_dir / manifest["tarball"]
        sha_file = artifacts_dir / f"{manifest['tarball']}.sha256"
        build_file = Path(manifest["_manifest_path"])
        shutil.copy2(tarball, out_dir / tarball.name)
        shutil.copy2(sha_file, out_dir / sha_file.name)
        shutil.copy2(build_file, out_dir / f"{manifest['artifact']}.build.json")
        public_manifest = {k: v for k, v in manifest.items() if not k.startswith("_")}
        public_manifest["tarball"] = f"{ARTIFACT_DIR}/{tarball.name}"
        copied.append(public_manifest)
    return copied


def nix_string(value: str) -> str:
    return json.dumps(value)


def package_attrs(manifests: list[dict[str, Any]]) -> str:
    by_system: dict[str, list[dict[str, Any]]] = {}
    for manifest in manifests:
        by_system.setdefault(manifest["system"], []).append(manifest)

    blocks = []
    for system in sorted(by_system):
        lines = [f'lib.optionalAttrs (system == "{system}") rec {{']
        for manifest in sorted(by_system[system], key=lambda m: m["artifact"]):
            attr = "remote-dev-hostctrl" if manifest["kind"] == "hostctrl" else "remote-dev"
            lines.extend(
                [
                    f"  {attr} = mkLocalBinaryPackage",
                    "    pkgs",
                    f"    {nix_string(attr)}",
                    f"    {nix_string(manifest['binary'])}",
                    f"    ./{manifest['tarball']};",
                ]
            )
        if any(m["kind"] == "hostctrl" for m in by_system[system]):
            lines.append("  remote-dev-kexec-installer = mkKexecInstallerPackage pkgs;")
        default = "remote-dev" if any(m["kind"] == "remote-dev" for m in by_system[system]) else "remote-dev-hostctrl"
        lines.append(f"  default = {default};")
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n          // ".join(blocks) if blocks else "{}"


def render_flake(repo_root: Path, branch_dir: Path, config: TargetConfig, manifests: list[dict[str, Any]], version: str) -> None:
    template = (repo_root / "templates/flake.nix.in").read_text()
    spec_attrs = "\n".join(
        f'          {arch} = "{HOST_IMAGE_SPEC_DIR}/{arch}.json";' for arch in config.host_arches
    )
    spec_values = "\n".join(
        f"        {arch} = builtins.fromJSON (builtins.readFile ./{HOST_IMAGE_SPEC_DIR}/{arch}.json);"
        for arch in config.host_arches
    )
    rendered = (
        template.replace("__VERSION__", nix_string(version))
        .replace("__HOST_IMAGE_SPEC_ATTRS__", spec_attrs)
        .replace("__HOST_IMAGE_SPEC_VALUES__", spec_values)
        .replace("__PACKAGES__", package_attrs(manifests))
    )
    unresolved = [token for token in ("__VERSION__", "__HOST_IMAGE_SPEC", "__PACKAGES__") if token in rendered]
    if unresolved:
        raise Fail(f"rendered flake contains unresolved placeholders: {unresolved}")
    (branch_dir / "flake.nix").write_text(rendered)
    lock_template = repo_root / "templates/flake.lock"
    if lock_template.is_file():
        shutil.copy2(lock_template, branch_dir / "flake.lock")


def public_flake_ref(config: TargetConfig) -> str:
    return f"github:{REPO}/{config.branch}"


def write_aws_bootstrap_flake(branch_dir: Path, config: TargetConfig, nixpkgs_rev: str) -> None:
    text = f"""{{
  description = "remote-dev AWS provider bootstrap";

  inputs = {{
    nixpkgs.url = "github:NixOS/nixpkgs/{nixpkgs_rev}";
  }};

  outputs = {{ nixpkgs, ... }}:
    let
      system = builtins.currentSystem or "x86_64-linux";
      remoteDevBin = builtins.getFlake {nix_string(public_flake_ref(config))};
    in
    {{
      nixosConfigurations.remote-dev-host = nixpkgs.lib.nixosSystem {{
        inherit system;
        modules = [
          remoteDevBin.nixosModules.remote-dev-host
          remoteDevBin.nixosModules.remote-dev-firstboot-register
          ({{ modulesPath, ... }}: {{
            imports = [ "${{modulesPath}}/virtualisation/amazon-image.nix" ];
            networking.hostName = "remote-dev-host";
            services.openssh.enable = true;
          }})
        ];
      }};
    }};
}}
"""
    path = branch_dir / AWS_BOOTSTRAP_FLAKE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if (branch_dir / "flake.lock").is_file():
        shutil.copy2(branch_dir / "flake.lock", branch_dir / AWS_BOOTSTRAP_LOCK)


def read_nixpkgs_rev(branch_dir: Path, repo_root: Path) -> str:
    for lock in (branch_dir / "flake.lock", repo_root / "templates/flake.lock"):
        if not lock.is_file():
            continue
        try:
            data = json_load(lock)
            rev = data["nodes"]["nixpkgs"]["locked"]["rev"]
            if isinstance(rev, str) and len(rev) == 40:
                return rev
        except (KeyError, TypeError, json.JSONDecodeError):
            pass
    return DEFAULT_NIXPKGS_REV


def aws_ami_arch(arch: str) -> str:
    if arch == "x86_64":
        return "x86_64"
    if arch == "aarch64":
        return "arm64"
    raise Fail(f"unsupported host arch {arch}")


def placeholder_store_path(system: str) -> str:
    return f"/nix/store/00000000000000000000000000000000-nixos-system-remote-dev-host-{system}"


def write_host_specs(
    branch_dir: Path,
    config: TargetConfig,
    nixpkgs_rev: str,
    trusted_public_key: str,
    toplevels: dict[str, str] | None = None,
) -> None:
    for arch in config.host_arches:
        system = f"{arch}-linux"
        spec = {
            "schema_version": HOST_IMAGE_SPEC_SCHEMA_VERSION,
            "baseline_id": HOST_CONFIG_ID,
            "image_contract_id": IMAGE_CONTRACT_ID,
            "arch": arch,
            "image_schema_version": HOST_IMAGE_SCHEMA_VERSION,
            "firstboot_schema_version": FIRSTBOOT_SCHEMA_VERSION,
            "disk_count": 1,
            "host_module": "nixosModules.remote-dev-host",
            "firstboot_module": "nixosModules.remote-dev-firstboot-register",
            "aws_official_nixos_ami": {
                "owner": "427812963091",
                "name_pattern": "nixos/*",
                "arch": aws_ami_arch(arch),
                "root_device_name": "/dev/xvda",
            },
            "bootstrap": {
                "flake_file": AWS_BOOTSTRAP_FLAKE,
                "lock_file": AWS_BOOTSTRAP_LOCK,
                "toplevel_attr": AWS_BOOTSTRAP_TOPLEVEL_ATTR,
                "nixpkgs_rev": nixpkgs_rev,
                "system": system,
                "toplevel_store_path": (toplevels or {}).get(system, placeholder_store_path(system)),
                "closure_manifest_file": f"cloud/aws-bootstrap-closure-{system}.json",
            },
            "nix_cache": {
                "substituter_url": f"https://raw.githubusercontent.com/{REPO}/{config.branch}/{NIX_CACHE_DIR}",
                "trusted_public_key": trusted_public_key,
            },
        }
        json_dump(branch_dir / HOST_IMAGE_SPEC_DIR / f"{arch}.json", spec)
        closure_path = branch_dir / spec["bootstrap"]["closure_manifest_file"]
        if not closure_path.is_file():
            closure = {
                "schema_version": 1,
                "system": system,
                "toplevel_store_path": spec["bootstrap"]["toplevel_store_path"],
                "paths": [spec["bootstrap"]["toplevel_store_path"]],
            }
            json_dump(closure_path, closure)


def write_nix_cache_info(branch_dir: Path, trusted_public_key: str) -> None:
    cache_dir = branch_dir / NIX_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.joinpath("nix-cache-info").write_text(
        "StoreDir: /nix/store\n"
        "WantMassQuery: 1\n"
        "Priority: 40\n"
        f"TrustedPublicKey: {trusted_public_key}\n"
    )


def version_string(config: TargetConfig, source_sha: str, source_ref: str) -> str:
    short = source_sha[:12]
    if config.name == "release":
        return f"release-{short}"
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    ref = source_ref.replace("/", "-")
    return f"host-service-test.{stamp}-g{short}-{ref}"


def maybe_realize_bootstrap_and_cache(
    branch_dir: Path, config: TargetConfig, signing_key: str | None, trusted_public_key: str
) -> dict[str, str]:
    if not signing_key:
        return {}
    toplevels: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="remote-dev-bin-bootstrap-") as tmp:
        tmp_dir = Path(tmp)
        flake = (branch_dir / AWS_BOOTSTRAP_FLAKE).read_text()
        flake = flake.replace(
            nix_string(public_flake_ref(config)), nix_string(f"path:{branch_dir}")
        )
        (tmp_dir / "flake.nix").write_text(flake)
        if (branch_dir / AWS_BOOTSTRAP_LOCK).is_file():
            shutil.copy2(branch_dir / AWS_BOOTSTRAP_LOCK, tmp_dir / "flake.lock")
        for arch in config.host_arches:
            system = f"{arch}-linux"
            attr = f"path:{tmp_dir}#{AWS_BOOTSTRAP_TOPLEVEL_ATTR}"
            output = run(
                [
                    "nix",
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "build",
                    "--impure",
                    "--system",
                    system,
                    "--print-out-paths",
                    "--no-link",
                    attr,
                ],
                cwd=branch_dir,
                capture=True,
            ).strip()
            if not output.startswith("/nix/store/"):
                raise Fail(f"nix build for {system} did not return a store path: {output}")
            output = output.splitlines()[-1]
            toplevels[system] = output
            closure = run(
                [
                    "nix",
                    "--extra-experimental-features",
                    "nix-command",
                    "path-info",
                    "--json",
                    "--recursive",
                    output,
                ],
                cwd=branch_dir,
                capture=True,
            )
            json_dump(
                branch_dir / f"cloud/aws-bootstrap-closure-{system}.json",
                {
                    "schema_version": 1,
                    "system": system,
                    "toplevel_store_path": output,
                    "paths": json.loads(closure),
                },
            )
    write_signed_nix_cache(branch_dir, signing_key, trusted_public_key, list(toplevels.values()))
    return toplevels


def write_signed_nix_cache(
    branch_dir: Path, signing_key: str, trusted_public_key: str, toplevels: list[str]
) -> None:
    roots = sorted(remote_dev_bin_cache_roots(branch_dir, toplevels))
    if not roots:
        return
    run_with_input(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "store",
            "sign",
            "--key-file",
            signing_key,
            "--stdin",
        ],
        "".join(f"{path}\n" for path in roots),
        cwd=branch_dir,
    )
    cache_dir = branch_dir / NIX_CACHE_DIR
    (cache_dir / "nar").mkdir(parents=True, exist_ok=True)
    key_name = nix_cache_key_name(trusted_public_key)
    for path in roots:
        write_narinfo(branch_dir, cache_dir, path, key_name)


def remote_dev_bin_cache_roots(branch_dir: Path, toplevels: list[str]) -> set[str]:
    roots = set(toplevels)
    for toplevel in toplevels:
        output = run(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command",
                "path-info",
                "--json",
                "--recursive",
                toplevel,
            ],
            cwd=branch_dir,
            capture=True,
        )
        for path, info in iter_nix_path_info(json.loads(output)):
            if path and remote_dev_bin_cache_path(path, info):
                roots.add(path)
    return roots


def iter_nix_path_info(raw: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(raw, dict):
        return [
            (path, info)
            for path, info in raw.items()
            if isinstance(path, str) and isinstance(info, dict)
        ]
    if isinstance(raw, list):
        paths: list[tuple[str, dict[str, Any]]] = []
        for info in raw:
            if not isinstance(info, dict):
                continue
            path = info.get("path") or info.get("storePath")
            if isinstance(path, str):
                paths.append((path, info))
        return paths
    raise Fail("nix path-info --json returned an unexpected shape")


def remote_dev_bin_cache_path(path: str, info: dict[str, Any] | None = None) -> bool:
    raw_name = store_path_name(path)
    _, sep, name = raw_name.partition("-")
    if not sep:
        raise Fail(f"{path} is not a valid hashed /nix/store path")
    return (
        "remote-dev" in name
        or nixos_bootstrap_generated_cache_path(name)
        or (info is not None and not cache_nixos_signed_path(info))
    )


def cache_nixos_signed_path(info: dict[str, Any]) -> bool:
    signatures = info.get("signatures") or []
    return any(
        isinstance(signature, str) and signature.startswith("cache.nixos.org-1:")
        for signature in signatures
    )


def nixos_bootstrap_generated_cache_path(name: str) -> bool:
    generated_roots = {
        "dbus-1",
        "etc",
        "etc-hostname",
        "etc-nix-registry.json",
        "etc-os-release",
        "etc-pam-environment",
        "etc-profile",
        "hosts",
        "issue",
        "nix.conf",
        "nixos-version",
        "set-environment",
        "sudoers",
        "system-path",
        "system-units",
        "tmpfiles.d",
        "user-units",
        "users-groups.json",
    }
    return name in generated_roots or name.startswith("initrd-linux-")


def write_narinfo(branch_dir: Path, cache_dir: Path, store_path: str, key_name: str) -> None:
    store_name = store_path_name(store_path)
    store_hash = store_name.split("-", 1)[0]
    info = path_info(branch_dir, store_path, store_name)
    nar_hash = nix_hash_sri_to_base32(branch_dir, info["narHash"])
    nar_size = int(info["narSize"])
    references = info.get("references") or []
    deriver = info.get("deriver") or None
    sigs = [sig for sig in info.get("signatures", []) if sig.startswith(f"{key_name}:")]
    if not sigs:
        raise Fail(f"signed path info for {store_path} did not include a signature from {key_name}")
    compressed = dump_compressed_nar(branch_dir, cache_dir, store_path, store_hash)
    file_hash = nix_hash_file_base32(branch_dir, compressed)
    final_nar = cache_dir / "nar" / f"{file_hash}.nar.xz"
    if compressed != final_nar:
        if final_nar.exists():
            compressed.unlink()
        else:
            compressed.rename(final_nar)
    file_size = final_nar.stat().st_size
    if file_size > GITHUB_MAX_BLOB_BYTES:
        raise Fail(
            f"remote-dev-bin cache entry for {store_path} is {file_size} bytes, "
            f"over GitHub's {GITHUB_MAX_BLOB_BYTES} byte blob limit"
        )
    lines = [
        f"StorePath: {store_path}",
        f"URL: nar/{file_hash}.nar.xz",
        f"Compression: xz",
        f"FileHash: sha256:{file_hash}",
        f"FileSize: {file_size}",
        f"NarHash: sha256:{nar_hash}",
        f"NarSize: {nar_size}",
        f"References: {' '.join(references)}",
    ]
    if deriver:
        lines.append(f"Deriver: {deriver}")
    for sig in sigs:
        lines.append(f"Sig: {sig}")
    (cache_dir / f"{store_hash}.narinfo").write_text("\n".join(lines) + "\n")


def path_info(branch_dir: Path, store_path: str, store_name: str) -> dict[str, Any]:
    output = run(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "path-info",
            "--json",
            "--json-format",
            "2",
            "--sigs",
            "--size",
            store_path,
        ],
        cwd=branch_dir,
        capture=True,
    )
    raw = json.loads(output)
    try:
        return raw["info"][store_name]
    except KeyError as error:
        raise Fail(f"nix path-info did not include {store_name}") from error


def dump_compressed_nar(branch_dir: Path, cache_dir: Path, store_path: str, store_hash: str) -> Path:
    nar_path = cache_dir / "nar" / f"{store_hash}.nar"
    compressed_path = nar_path.with_suffix(".nar.xz")
    for path in (nar_path, compressed_path):
        if path.exists():
            path.unlink()
    with nar_path.open("wb") as out:
        result = subprocess.run(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command",
                "store",
                "dump-path",
                store_path,
            ],
            cwd=branch_dir,
            check=False,
            stdout=out,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        raise Fail(f"nix store dump-path {store_path} failed: {result.stderr.decode().strip()}")
    run(["xz", "-z", "-9", "-f", str(nar_path)], cwd=branch_dir)
    return compressed_path


def nix_hash_file_base32(branch_dir: Path, path: Path) -> str:
    return run(
        ["nix", "hash", "file", "--type", "sha256", "--base32", str(path)],
        cwd=branch_dir,
        capture=True,
    ).strip()


def nix_hash_sri_to_base32(branch_dir: Path, value: str) -> str:
    return run(
        ["nix", "hash", "convert", "--from", "sri", "--to", "nix32", value],
        cwd=branch_dir,
        capture=True,
    ).strip()


def nix_cache_key_name(public_key: str) -> str:
    name, sep, _ = public_key.partition(":")
    if not sep or not name:
        raise Fail("Nix cache public key must use name:base64 form")
    return name


def store_path_name(path: str) -> str:
    if not path.startswith("/nix/store/"):
        raise Fail(f"{path} is not a /nix/store path")
    name = path.removeprefix("/nix/store/")
    if not name or "/" in name:
        raise Fail(f"{path} is not a direct /nix/store path")
    return name


def write_cloud_image_metadata(
    branch_dir: Path,
    args: argparse.Namespace,
    config: TargetConfig,
    image_manifest: dict[str, Any] | None,
) -> None:
    if not args.image_digest:
        return
    image_ref = args.image or ""
    digest = args.image_digest
    deployed = args.deployed_image or (f"{image_ref.split(':')[0]}@{digest}" if image_ref else digest)
    metadata = {
        "schema_version": 1,
        "target": config.name,
        "cloud": config.cloud,
        "project": config.project,
        "source_repo": SOURCE_REPO,
        "source_ref": args.source_ref,
        "source_sha": args.source_sha,
        "image": image_ref,
        "digest": digest,
        "deployed_image": deployed,
        "deployment": {
            "platform": "cloud-run",
            "service": "remote-dev-host-service",
            "region": "us-west1",
        },
        "workflow_run_id": args.workflow_run_id,
        "generated_at": utc_now(),
    }
    if image_manifest:
        metadata["build"] = {k: v for k, v in image_manifest.items() if not k.startswith("_")}
    json_dump(branch_dir / "cloud/host-service-image.json", metadata)


def cmd_render_tree(args: argparse.Namespace) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    artifacts_dir = Path(args.artifacts_dir).resolve()
    branch_dir = Path(args.branch_dir).resolve()
    config = target_config(args.target, args.arch)
    branch_dir.mkdir(parents=True, exist_ok=True)
    remove_generated_paths(branch_dir)

    manifests = collect_build_manifests(artifacts_dir)
    binaries = select_binary_manifests(manifests, artifacts_dir, config, args.source_sha)
    image_manifests = [m for m in manifests if m.get("kind") == "host-service-image"]
    for manifest in image_manifests:
        if manifest.get("source_sha") != args.source_sha:
            raise Fail(f"{manifest['_manifest_path']}: source_sha mismatch")
        if manifest.get("target") != config.name:
            raise Fail(f"{manifest['_manifest_path']}: target mismatch")
        tarball = artifacts_dir / manifest.get("tarball", "")
        if not tarball.is_file():
            raise Fail(f"{manifest['_manifest_path']}: image tarball is missing")
        if sha256_file(tarball) != manifest.get("sha256"):
            raise Fail(f"{manifest['_manifest_path']}: image tarball sha256 mismatch")

    copied = copy_artifacts(artifacts_dir, branch_dir, binaries)
    trusted_key = args.nix_cache_public_key or "remote-dev-placeholder:public"
    version = args.version or version_string(config, args.source_sha, args.source_ref)
    render_flake(repo_root, branch_dir, config, copied, version)
    nixpkgs_rev = read_nixpkgs_rev(branch_dir, repo_root)
    write_aws_bootstrap_flake(branch_dir, config, nixpkgs_rev)
    write_nix_cache_info(branch_dir, trusted_key)
    toplevels = maybe_realize_bootstrap_and_cache(
        branch_dir, config, args.nix_cache_signing_key_file, trusted_key
    )
    write_host_specs(branch_dir, config, nixpkgs_rev, trusted_key, toplevels)
    write_cloud_image_metadata(
        branch_dir, args, config, image_manifests[0] if image_manifests else None
    )

    aggregate = {
        "schema_version": 1,
        "target": {
            "name": config.name,
            "branch": config.branch,
            "cloud": config.cloud,
            "project": config.project,
        },
        "source": {
            "repo": SOURCE_REPO,
            "ref": args.source_ref,
            "sha": args.source_sha,
        },
        "remote_dev_systems": list(config.remote_dev_systems),
        "host_arches": list(config.host_arches),
        "artifacts": copied,
        "host_service_image": "cloud/host-service-image.json"
        if (branch_dir / "cloud/host-service-image.json").is_file()
        else None,
        "retention": {
            "max_commits": config.retention_commits,
            "max_days": config.retention_days,
        },
        "generated_at": utc_now(),
    }
    json_dump(branch_dir / "build-manifest.json", aggregate)
    validate_tree(branch_dir, config)


def validate_tree(branch_dir: Path, config: TargetConfig) -> None:
    for relative in [
        "build-manifest.json",
        "flake.nix",
        "flake.lock",
        AWS_BOOTSTRAP_FLAKE,
        AWS_BOOTSTRAP_LOCK,
        f"{NIX_CACHE_DIR}/nix-cache-info",
    ]:
        if not (branch_dir / relative).is_file():
            raise Fail(f"missing required branch artifact {relative}")
    flake = (branch_dir / "flake.nix").read_text()
    if "releases/download" in flake or "fetchurl" in flake:
        raise Fail("flake.nix must consume repo-local artifacts, not GitHub Release URLs")
    manifest = json_load(branch_dir / "build-manifest.json")
    if manifest["target"]["branch"] != config.branch:
        raise Fail("build-manifest target branch mismatch")
    for artifact in manifest["artifacts"]:
        tarball = branch_dir / artifact["tarball"]
        if not tarball.is_file():
            raise Fail(f"missing artifact {artifact['tarball']}")
        actual = sha256_file(tarball)
        if actual != artifact["sha256"]:
            raise Fail(f"{artifact['tarball']}: sha256 mismatch")
        for suffix in (".sha256", ".build.json"):
            expected = branch_dir / ARTIFACT_DIR / f"{artifact['artifact']}.tar.gz{suffix}"
            if suffix == ".build.json":
                expected = branch_dir / ARTIFACT_DIR / f"{artifact['artifact']}.build.json"
            if not expected.is_file():
                raise Fail(f"missing artifact sidecar {expected.relative_to(branch_dir)}")
    for arch in config.host_arches:
        spec_path = branch_dir / HOST_IMAGE_SPEC_DIR / f"{arch}.json"
        if not spec_path.is_file():
            raise Fail(f"missing host image spec {spec_path.relative_to(branch_dir)}")
        spec = json_load(spec_path)
        if spec.get("arch") != arch:
            raise Fail(f"{spec_path}: arch mismatch")
        if spec.get("schema_version") != HOST_IMAGE_SPEC_SCHEMA_VERSION:
            raise Fail(f"{spec_path}: schema_version mismatch")
        closure = branch_dir / spec["bootstrap"]["closure_manifest_file"]
        if not closure.is_file():
            raise Fail(f"{spec_path}: closure manifest is missing")
    missing_arches = {"x86_64", "aarch64"} - set(config.host_arches)
    for arch in missing_arches:
        if (branch_dir / HOST_IMAGE_SPEC_DIR / f"{arch}.json").exists():
            raise Fail(f"unexpected host image spec for unbuilt arch {arch}")


def cmd_validate_tree(args: argparse.Namespace) -> None:
    validate_tree(Path(args.branch_dir), target_config(args.target, args.arch))


def cmd_cleanup_test_branch(args: argparse.Namespace) -> None:
    branch_dir = Path(args.branch_dir)
    config = target_config("host-service-test", "both")
    if args.branch != config.branch:
        raise Fail("retention cleanup is only allowed for host-service-test")
    run(["git", "checkout", config.branch], cwd=branch_dir)
    commits = run(
        ["git", "rev-list", "--first-parent", config.branch], cwd=branch_dir, capture=True
    ).splitlines()
    if not commits:
        return
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.max_days)
    latest_kept = commits[min(len(commits), args.max_commits) - 1]
    latest_kept_ts = int(
        run(["git", "show", "-s", "--format=%ct", latest_kept], cwd=branch_dir, capture=True)
    )
    within_limits = len(commits) <= args.max_commits and dt.datetime.fromtimestamp(
        latest_kept_ts, dt.UTC
    ) >= cutoff
    if within_limits:
        return
    # Keep cleanup simple and explicit: the workflow force-pushes the newest tree
    # after creating an orphan branch. This never runs for main.
    run(["git", "checkout", "--orphan", "host-service-test-retained"], cwd=branch_dir)
    run(["git", "add", "-A"], cwd=branch_dir)
    run(["git", "commit", "-m", "host-service-test: retain latest generated artifact tree"], cwd=branch_dir)
    run(["git", "branch", "-M", "host-service-test-retained", config.branch], cwd=branch_dir)


def fake_artifact(root: Path, manifest: dict[str, str]) -> None:
    name = manifest["artifact"]
    tarball = root / f"{name}.tar.gz"
    tarball.write_bytes(f"fake {name}\n".encode())
    digest = sha256_file(tarball)
    (root / f"{name}.tar.gz.sha256").write_text(f"{digest}  {name}.tar.gz\n")
    data = {
        "schema_version": 1,
        "kind": manifest["kind"],
        "target": manifest["target"],
        "source_repo": SOURCE_REPO,
        "source_ref": "test-source",
        "source_sha": "0123456789abcdef0123456789abcdef01234567",
        "system": manifest["system"],
        "arch": manifest["arch"],
        "cargo_target": manifest["cargo_target"],
        "package": manifest["package"],
        "binary": manifest["binary"],
        "artifact": name,
        "tarball": tarball.name,
        "sha256": digest,
        "generated_at": utc_now(),
    }
    json_dump(root / f"{name}.build.json", data)


def assert_workflows(repo_root: Path) -> None:
    test = (repo_root / ".github/workflows/publish-test.yml").read_text()
    release = (repo_root / ".github/workflows/publish-release.yml").read_text()
    cleanup = (repo_root / ".github/workflows/cleanup-host-service-test.yml").read_text()
    combined = "\n".join([test, release, cleanup])
    if "pull_request" in combined or "pull_request_target" in combined:
        raise Fail("publish workflows must not run on pull_request events")
    for forbidden in ("cloud:", "project:"):
        if forbidden in test.split("jobs:", 1)[0]:
            raise Fail("publish-test must not expose cloud/project inputs")
        if forbidden in release.split("jobs:", 1)[0]:
            raise Fail("publish-release must not expose cloud/project inputs")
    if "environment: prod" not in release:
        raise Fail("publish-release must use the protected prod environment")
    if "REMOTE_DEV_CONFIRM_PROD" not in release or "remote-dev-host-prod" not in release:
        raise Fail("publish-release must carry the prod confirmation guard")
    if "contents: write" in test.split("publish:", 1)[0]:
        raise Fail("test build jobs must not receive contents write")
    if "git add -A" not in test or "git add -A" not in release:
        raise Fail("publish workflows must stage generated deletions with git add -A")


def cmd_self_test(_: argparse.Namespace) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    release = target_config("release")
    test_arm = target_config("host-service-test", "aarch64")
    if release.branch != "main" or release.cloud != "prod" or release.confirm_prod != "remote-dev-host-prod":
        raise Fail("release target is not bound to main/prod")
    if test_arm.remote_dev_systems != ("aarch64-linux",) or test_arm.host_arches != ("aarch64",):
        raise Fail("test aarch64 target did not narrow the matrix")
    if len(build_matrix(release)["include"]) != 6:
        raise Fail("release matrix must include four remote-dev binaries and two hostctrl binaries")
    cache_examples = {
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-remote-dev-hostctrl-test": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-etc": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-system-path": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-users-groups.json": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-initrd-linux-6.18.31": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-etc-hostname": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-tmpfiles.d": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-sudoers": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-user-units": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-issue": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-hosts": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-set-environment": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-etc-profile": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nix.conf": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-etc-os-release": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-etc-nix-registry.json": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-etc-pam-environment": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-system-units": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nixos-version": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-dbus-1": True,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-linux-6.18.31": False,
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc-2.42-61": False,
    }
    for path, expected in cache_examples.items():
        if remote_dev_bin_cache_path(path) != expected:
            raise Fail(f"unexpected cache root decision for {path}")
    if not remote_dev_bin_cache_path(
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-unit-nix-daemon.service",
        {"signatures": []},
    ):
        raise Fail("paths without cache.nixos.org signatures must be cached")
    if remote_dev_bin_cache_path(
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc-2.42-61",
        {"signatures": ["cache.nixos.org-1:test"]},
    ):
        raise Fail("cache.nixos.org-signed paths should not be mirrored by default")
    if iter_nix_path_info(
        {
            "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-remote-dev-test": {
                "signatures": []
            }
        }
    ) != [
        (
            "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-remote-dev-test",
            {"signatures": []},
        )
    ]:
        raise Fail("unexpected object-shaped nix path-info parsing")
    assert_workflows(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        artifacts = tmp_path / "artifacts"
        branch = tmp_path / "branch"
        artifacts.mkdir()
        for entry in build_matrix(test_arm)["include"]:
            fake_artifact(artifacts, {**entry, "target": "host-service-test"})
        ns = argparse.Namespace(
            target="host-service-test",
            arch="aarch64",
            artifacts_dir=str(artifacts),
            branch_dir=str(branch),
            source_sha="0123456789abcdef0123456789abcdef01234567",
            source_ref="test-source",
            version=None,
            nix_cache_public_key="remote-dev-test:public",
            nix_cache_signing_key_file=None,
            image_digest="sha256:" + "1" * 64,
            image="us-west1-docker.pkg.dev/remote-dev-host-test/remote-dev/host-service:test",
            deployed_image=None,
            workflow_run_id="1",
        )
        cmd_render_tree(ns)
        if (branch / "host-image-specs/x86_64.json").exists():
            raise Fail("aarch64-only publish rendered x86_64 host spec")
    print("self-test passed")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("matrix")
    m.add_argument("--target", choices=["release", "host-service-test"], required=True)
    m.add_argument("--arch", choices=["x86_64", "aarch64", "both"], default="both")
    m.set_defaults(func=cmd_matrix)

    b = sub.add_parser("write-binary-manifest")
    for name in [
        "target",
        "kind",
        "source-ref",
        "source-sha",
        "system",
        "arch",
        "cargo-target",
        "package",
        "binary",
        "artifact",
        "tarball",
        "output",
    ]:
        b.add_argument(f"--{name}", required=True)
    b.set_defaults(func=cmd_write_binary_manifest)

    i = sub.add_parser("write-image-manifest")
    for name in ["target", "source-ref", "source-sha", "tarball", "output"]:
        i.add_argument(f"--{name}", required=True)
    i.set_defaults(func=cmd_write_image_manifest)

    r = sub.add_parser("render-tree")
    r.add_argument("--target", choices=["release", "host-service-test"], required=True)
    r.add_argument("--arch", choices=["x86_64", "aarch64", "both"], default="both")
    r.add_argument("--artifacts-dir", required=True)
    r.add_argument("--branch-dir", required=True)
    r.add_argument("--source-ref", required=True)
    r.add_argument("--source-sha", required=True)
    r.add_argument("--version")
    r.add_argument("--nix-cache-public-key")
    r.add_argument("--nix-cache-signing-key-file")
    r.add_argument("--image")
    r.add_argument("--image-digest")
    r.add_argument("--deployed-image")
    r.add_argument("--workflow-run-id")
    r.set_defaults(func=cmd_render_tree)

    v = sub.add_parser("validate-tree")
    v.add_argument("--target", choices=["release", "host-service-test"], required=True)
    v.add_argument("--arch", choices=["x86_64", "aarch64", "both"], default="both")
    v.add_argument("--branch-dir", required=True)
    v.set_defaults(func=cmd_validate_tree)

    c = sub.add_parser("cleanup-test-branch")
    c.add_argument("--branch-dir", required=True)
    c.add_argument("--branch", required=True)
    c.add_argument("--max-commits", type=int, default=5)
    c.add_argument("--max-days", type=int, default=7)
    c.set_defaults(func=cmd_cleanup_test_branch)

    s = sub.add_parser("self-test")
    s.set_defaults(func=cmd_self_test)
    return p


def main(argv: list[str]) -> int:
    try:
        args = parser().parse_args(argv)
        args.func(args)
        return 0
    except Fail as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
