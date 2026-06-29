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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


REPO = "M-Adoo/remote-dev-bin"
SOURCE_REPO = "M-Adoo/remote-dev"
HOST_CONFIG_ID = "remote-dev-agent-runtime-v2"
IMAGE_CONTRACT_ID = "remote-dev-cloud-host-v1"
HOST_IMAGE_SPEC_SCHEMA_VERSION = 9
HOST_IMAGE_SCHEMA_VERSION = 3
FIRSTBOOT_SCHEMA_VERSION = 2
HOST_GROUPS_CATALOG_SCHEMA_VERSION = 2
AWS_BOOTSTRAP_FLAKE = "cloud/aws-bootstrap-flake.nix"
AWS_BOOTSTRAP_LOCK = "cloud/aws-bootstrap-flake.lock"
NIX_CACHE_DIR = "nix-cache"
ARTIFACT_DIR = "artifacts"
HOST_IMAGE_SPEC_DIR = "host-runtime-specs"
HOST_GROUPS_DIR = "cloud/host-groups"
DEFAULT_NIXPKGS_REV = "0000000000000000000000000000000000000000"
GITHUB_MAX_BLOB_BYTES = 100_000_000
BOOTSTRAP_SOURCE_MAX_BYTES = 5 * 1024 * 1024
CLOSURE_AUDIT_TOP_NAR_PATHS = 10
MAX_AGENT_RUNTIME_CLOSURE_PATHS = 3
REMOTE_CACHE_NARINFO_MAX_BYTES = 256 * 1024
CLOSURE_AUDIT_MARKERS = (
    "amazon-ssm-agent",
    "git-doc",
    "nixos-manual-html",
    "nix-manual",
)
AWS_AMI_INDEX_URL = "https://nixos.github.io/amis/images.json"
AWS_AMI_PIN_METADATA = "templates/aws-ami-pin.json"
AWS_AMI_REV_PREFIX_LEN = 12
AWS_AMI_PREFERRED_RELEASE_PREFIX = "25."
NIXOS_AMI_NAME_RE = re.compile(r"^nixos/.+\.([0-9a-f]{12})-(x86_64|aarch64)-linux$")
AGENT_RUNTIME_CLOSURE_ALLOWED_NAMES = (
    "remote-dev-agent-runtime",
    "remote-dev-",
    "remote-dev-runtime",
)
AGENT_RUNTIME_CLOSURE_DENIED_MARKERS = (
    "binutils",
    "clang",
    "gcc",
    "glibc",
    "host-group",
    "libgcc",
    "stdenv",
)
CLOUD_RUN_RFC3339_RE = re.compile(
    r"^(?P<datetime>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)
CLOUD_RUN_LATEST_READY_FIELDS = ("latestReadyRevisionName", "latestReadyRevision")
CLOUD_RUN_LATEST_CREATED_FIELDS = ("latestCreatedRevisionName", "latestCreatedRevision")
CLOUD_RUN_TRAFFIC_REVISION_FIELDS = ("revisionName", "revision")

GIT_CORE_COMMANDS = (
    "git",
    "git-cvsserver",
    "git-http-backend",
    "git-jump",
    "git-receive-pack",
    "git-shell",
    "git-upload-archive",
    "git-upload-pack",
    "scalar",
)

HOST_BASE_COMMANDS = (
    "bash",
    "curl",
    "ip",
    "nix",
    "nix-store",
    "scp",
    "ssh",
    "ssh-keygen",
    "systemctl",
    "systemd-nspawn",
    "nsenter",
)


HOST_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "host-base-tools",
        "priority": 0,
        "labels": ["host-base", "host-tools", "host-default"],
        "inputs": [
            "pkgs.bash",
            "pkgs.coreutils",
            "pkgs.curl",
            "pkgs.iproute2",
            "pkgs.nix",
            "pkgs.openssh",
            "pkgs.systemd",
            "pkgs.util-linux",
        ],
        "commands": list(HOST_BASE_COMMANDS),
    },
    {
        "id": "git-core",
        "priority": 10,
        "labels": ["git", "workspace-sync", "bootstrap"],
        "inputs": ["pkgs.gitMinimal"],
        "commands": list(GIT_CORE_COMMANDS),
    },
    {
        "id": "mosh-transport",
        "priority": 5,
        "labels": ["terminal", "mosh", "transport"],
        "inputs": ["pkgs.mosh"],
        "commands": ["mosh-server"],
    },
    {
        "id": "default-dev-shell-prefill",
        "priority": 10,
        "labels": [
            "default-shell",
            "store-prefill",
            "workspace-eager-prefill",
        ],
        "inputs": ["pkgs.bashInteractive", "pkgs.stdenv.cc.cc.lib"],
        "commands": [],
    },
    {
        "id": "nix-source-baseline",
        "priority": 20,
        "labels": ["nix", "source", "store-prefill"],
        "inputs": ["nixSourceBaseline"],
        "commands": [],
    },
    {
        "id": "shell-startup",
        "priority": 30,
        "labels": ["shell-baseline", "shell", "interactive", "startup"],
        "inputs": ["pkgs.zsh", "pkgs.starship"],
        "commands": ["zsh", "starship"],
    },
    {
        "id": "shell-extras",
        "priority": 40,
        "labels": ["shell", "interactive", "extras"],
        "inputs": ["pkgs.fzf"],
        "commands": ["fzf"],
    },
    {
        "id": "vscode-compat",
        "priority": 45,
        "labels": ["vscode", "editor", "compat"],
        "inputs": ["pkgs.patchelf"],
        "commands": ["patchelf"],
    },
    {
        "id": "compression-tools",
        "priority": 50,
        "labels": ["compression", "archive"],
        "inputs": ["pkgs.zstd"],
        "commands": ["zstd"],
    },
    {
        "id": "build-baseline",
        "priority": 60,
        "labels": ["build", "tooling", "store-prefill", "workspace-eager-prefill"],
        "inputs": ["pkgs.gnumake", "pkgs.pkg-config"],
        "commands": ["pkg-config", "make"],
    },
    {
        "id": "c-toolchain-gcc",
        "priority": 70,
        "labels": ["diagnostic", "cc", "gcc", "build-debug", "background"],
        "inputs": ["pkgs.gcc"],
        "commands": ["cc", "gcc"],
    },
    {
        "id": "c-toolchain-clang",
        "priority": 200,
        "labels": ["diagnostic", "clang", "build-debug", "background"],
        "inputs": ["pkgs.clang"],
        "commands": ["clang"],
    },
    {
        "id": "dev-diagnostics",
        "priority": 80,
        "labels": ["diagnostic", "build-debug", "background"],
        "inputs": ["pkgs.file", "pkgs.glibc.bin", "pkgs.strace"],
        "commands": ["strace", "file", "ldd"],
    },
)


def validate_host_group_model() -> None:
    seen_ids: set[str] = set()
    seen_inputs: dict[str, str] = {}
    seen_commands: dict[str, str] = {}
    for group in HOST_GROUPS:
        group_id = group.get("id")
        if not isinstance(group_id, str) or not group_id:
            raise Fail("host group id must be a non-empty string")
        if group_id in seen_ids:
            raise Fail(f"duplicate host group id {group_id}")
        seen_ids.add(group_id)
        inputs = group.get("inputs")
        if not isinstance(inputs, list):
            raise Fail(f"host group {group_id} inputs must be a list")
        for package in inputs:
            if not isinstance(package, str) or not package:
                raise Fail(f"host group {group_id} has invalid input {package!r}")
            owner = seen_inputs.get(package)
            if owner is not None:
                raise Fail(f"host group input {package} is owned by both {owner} and {group_id}")
            seen_inputs[package] = group_id
        commands = group.get("commands")
        if not isinstance(commands, list):
            raise Fail(f"host group {group_id} commands must be a list")
        for command in commands:
            if not isinstance(command, str) or not command:
                raise Fail(f"host group {group_id} has invalid command {command!r}")
            owner = seen_commands.get(command)
            if owner is not None:
                raise Fail(f"host group command {command} is owned by both {owner} and {group_id}")
            seen_commands[command] = group_id


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
    def runtime_artifact(self) -> str:
        return f"remote-dev-runtime-{self.system}"


SYSTEMS: dict[str, SystemTarget] = {
    "x86_64-linux": SystemTarget(
        "x86_64-linux", "x86_64", "x86_64-unknown-linux-musl", "ubuntu-latest"
    ),
    "aarch64-linux": SystemTarget(
        "aarch64-linux", "aarch64", "aarch64-unknown-linux-musl", "ubuntu-24.04-arm"
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


@dataclass(frozen=True)
class AwsAmiPinImage:
    region: str
    host_arch: str
    aws_arch: str
    name: str
    image_id: str
    creation_date: str
    rev_prefix: str


@dataclass(frozen=True)
class AwsAmiPinSelection:
    rev_prefix: str
    latest_creation_date: str
    sample_names: dict[str, str]
    regions_by_arch: dict[str, tuple[str, ...]]
    images_by_arch: dict[str, dict[str, AwsAmiPinImage]]
    candidate_summary: list[dict[str, Any]]


@dataclass(frozen=True)
class CachePathRef:
    label: str
    path: str
    info: dict[str, Any] | None
    local_required: bool


@dataclass(frozen=True)
class RemoteCacheEntry:
    label: str
    store_path: str
    narinfo_path: Path
    narinfo_relative: str
    nar_relative: str


@dataclass(frozen=True)
class CloudRunRevision:
    name: str
    created_at: dt.datetime
    created_at_raw: str


@dataclass(frozen=True)
class CloudRunRevisionCleanupPlan:
    total_revisions: int
    keep_latest: int
    protected_revisions: dict[str, tuple[str, ...]]
    delete_revisions: tuple[CloudRunRevision, ...]

    @property
    def retained_after_cleanup(self) -> int:
        return self.total_revisions - len(self.delete_revisions)


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


def assert_static_linux_elf_report(label: str, report: str) -> None:
    for forbidden in ("INTERP", "NEEDED"):
        if forbidden in report:
            raise Fail(f"{label}: Linux artifact must be static; readelf reported {forbidden}")


def cmd_audit_binary(args: argparse.Namespace) -> None:
    binary = Path(args.binary)
    if not binary.is_file():
        raise Fail(f"missing binary {binary}")
    if not args.cargo_target.endswith("linux-musl"):
        return
    program_headers = run(["readelf", "-l", str(binary)], capture=True)
    dynamic = run(["readelf", "-d", str(binary)], capture=True)
    assert_static_linux_elf_report(
        f"{args.artifact} ({args.cargo_target})",
        f"{program_headers}\n{dynamic}",
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
                "kind": "runtime",
                "package": "remote-dev",
                "binary": "remote-dev-runtime",
                "system": target.system,
                "arch": target.arch,
                "cargo_target": target.cargo_target,
                "os": target.os,
                "artifact": target.runtime_artifact,
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
    names.update(SYSTEMS[f"{arch}-linux"].runtime_artifact for arch in config.host_arches)
    return names


def select_binary_manifests(
    manifests: list[dict[str, Any]], artifacts_dir: Path, config: TargetConfig, source_sha: str
) -> list[dict[str, Any]]:
    binaries = [m for m in manifests if m.get("kind") in ("remote-dev", "runtime")]
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
        "host-image-specs",
        "host-image-spec.json",
        "host-service-test-metadata.json",
        "cloud-images.json",
        "remote-dev-x86_64-linux.tar.gz",
        "remote-dev-x86_64-linux.tar.gz.sha256",
        "remote-dev-aarch64-linux.tar.gz",
        "remote-dev-aarch64-linux.tar.gz.sha256",
        "remote-dev-x86_64-darwin.tar.gz",
        "remote-dev-x86_64-darwin.tar.gz.sha256",
        "remote-dev-aarch64-darwin.tar.gz",
        "remote-dev-aarch64-darwin.tar.gz.sha256",
        "cloud/host-service-image.json",
        "cloud/host-groups-catalog-x86_64-linux.json",
        "cloud/host-groups-catalog-aarch64-linux.json",
        HOST_GROUPS_DIR,
        "cloud/aws-builder-flake.nix",
        "cloud/aws-builder-flake.lock",
        AWS_BOOTSTRAP_FLAKE,
        AWS_BOOTSTRAP_LOCK,
        "cloud/aws-bootstrap-closure-x86_64-linux.json",
        "cloud/aws-bootstrap-closure-aarch64-linux.json",
        "cloud/agent-runtime-closure-x86_64-linux.json",
        "cloud/agent-runtime-closure-aarch64-linux.json",
        NIX_CACHE_DIR,
    ]:
        path = branch_dir / relative
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    for path in branch_dir.glob("cloud/*runtime-closure-*-linux.json"):
        if path.is_file():
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
            attr = {
                "remote-dev": "remote-dev",
                "runtime": "remote-dev-runtime",
            }[manifest["kind"]]
            lines.extend(
                [
                    f"  {attr} = mkLocalBinaryPackage",
                    "    pkgs",
                    f"    {nix_string(attr)}",
                    f"    {nix_string(manifest['binary'])}",
                    f"    ./{manifest['tarball']};",
                ]
            )
        if {m["kind"] for m in by_system[system]} >= {"remote-dev", "runtime"}:
            lines.append(
                "  remote-dev-agent-runtime = mkAgentRuntimePackage pkgs remote-dev remote-dev-runtime;"
            )
            for group in HOST_GROUPS:
                attr = host_group_package_attr(group["id"])
                lines.append(f'  "{attr}" = (hostGroupPackages system pkgs)."{group["id"]}";')
        if any(m["kind"] == "runtime" for m in by_system[system]):
            lines.append("  remote-dev-kexec-installer = mkKexecInstallerPackage pkgs;")
        default = "remote-dev" if any(m["kind"] == "remote-dev" for m in by_system[system]) else "remote-dev-runtime"
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


def aws_ami_host_arch_from_name(name_arch: str) -> str:
    if name_arch == "x86_64":
        return "x86_64"
    if name_arch == "aarch64":
        return "aarch64"
    raise Fail(f"unsupported NixOS AMI name arch {name_arch}")


def read_json_url(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "remote-dev-bin-publish"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise Fail(f"fetch {url}: {error}") from error


def read_aws_ami_index(images_json: str | None) -> Any:
    if images_json:
        return json_load(Path(images_json))
    return read_json_url(AWS_AMI_INDEX_URL)


def aws_ami_index_regions(index: Any) -> tuple[str, ...]:
    if not isinstance(index, dict):
        raise Fail("AWS AMI index must be a region object")
    regions = sorted(
        region
        for region, payload in index.items()
        if isinstance(region, str)
        and isinstance(payload, dict)
        and isinstance(payload.get("Images"), list)
    )
    if not regions:
        raise Fail("AWS AMI index has no regions with Images")
    return tuple(regions)


def iter_aws_ami_pin_images(index: Any) -> list[AwsAmiPinImage]:
    images: list[AwsAmiPinImage] = []
    for region in aws_ami_index_regions(index):
        for raw in index[region]["Images"]:
            if not isinstance(raw, dict):
                continue
            name = raw.get("Name")
            if not isinstance(name, str):
                continue
            match = NIXOS_AMI_NAME_RE.match(name)
            if not match:
                continue
            rev_prefix, name_arch = match.groups()
            host_arch = aws_ami_host_arch_from_name(name_arch)
            if raw.get("Architecture") != aws_ami_arch(host_arch):
                continue
            if raw.get("State") != "available":
                continue
            if raw.get("RootDeviceName") != "/dev/xvda":
                continue
            image_id = raw.get("ImageId")
            creation_date = raw.get("CreationDate")
            if not isinstance(image_id, str) or not isinstance(creation_date, str):
                continue
            images.append(
                AwsAmiPinImage(
                    region=region,
                    host_arch=host_arch,
                    aws_arch=aws_ami_arch(host_arch),
                    name=name,
                    image_id=image_id,
                    creation_date=creation_date,
                    rev_prefix=rev_prefix,
                )
            )
    if not images:
        raise Fail("AWS AMI index has no parseable official NixOS AMI names")
    return images


def select_aws_ami_pin(index: Any, host_arches: tuple[str, ...]) -> AwsAmiPinSelection:
    required_regions = set(aws_ami_index_regions(index))
    required_arches = tuple(dict.fromkeys(host_arches))
    by_prefix: dict[str, dict[str, dict[str, AwsAmiPinImage]]] = {}
    for image in iter_aws_ami_pin_images(index):
        if image.host_arch not in required_arches:
            continue
        region_images = by_prefix.setdefault(image.rev_prefix, {}).setdefault(image.host_arch, {})
        existing = region_images.get(image.region)
        if existing is None or image.creation_date > existing.creation_date:
            region_images[image.region] = image

    candidates: list[dict[str, Any]] = []
    complete: list[tuple[str, dict[str, dict[str, AwsAmiPinImage]], str]] = []
    for rev_prefix, by_arch in by_prefix.items():
        missing_by_arch = {
            arch: sorted(required_regions - set(by_arch.get(arch, {})))
            for arch in required_arches
        }
        latest_creation_date = max(
            (
                image.creation_date
                for region_images in by_arch.values()
                for image in region_images.values()
            ),
            default="",
        )
        candidates.append(
            {
                "rev_prefix": rev_prefix,
                "latest_creation_date": latest_creation_date,
                "coverage": {
                    arch: len(by_arch.get(arch, {}))
                    for arch in required_arches
                },
                "missing_regions": missing_by_arch,
            }
        )
        if all(not missing for missing in missing_by_arch.values()):
            complete.append((rev_prefix, by_arch, latest_creation_date))

    candidates.sort(key=lambda candidate: (candidate["latest_creation_date"], candidate["rev_prefix"]), reverse=True)
    if not complete:
        summary = json.dumps(candidates[:8], indent=2, sort_keys=True)
        raise Fail(
            "no official NixOS AWS AMI rev prefix has full region coverage for "
            f"{', '.join(required_arches)} across {len(required_regions)} regions; "
            f"top candidates:\n{summary}"
        )

    complete.sort(key=lambda item: (item[2], item[0]), reverse=True)
    rev_prefix, by_arch, latest_creation_date = complete[0]
    return AwsAmiPinSelection(
        rev_prefix=rev_prefix,
        latest_creation_date=latest_creation_date,
        sample_names={
            arch: sorted(by_arch[arch].values(), key=lambda image: image.creation_date, reverse=True)[0].name
            for arch in required_arches
        },
        regions_by_arch={
            arch: tuple(sorted(by_arch[arch]))
            for arch in required_arches
        },
        images_by_arch=by_arch,
        candidate_summary=candidates[:8],
    )


def resolve_nixpkgs_full_rev(rev_prefix: str) -> str:
    if len(rev_prefix) != AWS_AMI_REV_PREFIX_LEN:
        raise Fail(f"expected {AWS_AMI_REV_PREFIX_LEN} character AMI rev prefix, got {rev_prefix}")
    url = f"https://api.github.com/repos/NixOS/nixpkgs/commits/{rev_prefix}"
    data = read_json_url(url)
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or len(sha) != 40 or not sha.startswith(rev_prefix):
        raise Fail(f"GitHub did not resolve {rev_prefix} to a matching full nixpkgs commit")
    return sha


def is_full_commit_sha(value: str) -> bool:
    return len(value) == 40 and all(byte in "0123456789abcdefABCDEF" for byte in value)


def nixpkgs_locked_metadata(full_rev: str) -> dict[str, Any]:
    raw = run(
        [
            "nix",
            "flake",
            "metadata",
            "--json",
            "--extra-experimental-features",
            "nix-command",
            "--extra-experimental-features",
            "flakes",
            f"github:NixOS/nixpkgs/{full_rev}",
        ],
        capture=True,
    )
    try:
        locked = json.loads(raw)["locked"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise Fail(f"could not read nixpkgs lock metadata for {full_rev}: {error}") from error
    expected = {
        "lastModified": int,
        "narHash": str,
        "owner": str,
        "repo": str,
        "rev": str,
        "type": str,
    }
    for key, kind in expected.items():
        if not isinstance(locked.get(key), kind):
            raise Fail(f"nixpkgs metadata for {full_rev} is missing {key}")
    if locked["owner"] != "NixOS" or locked["repo"] != "nixpkgs" or locked["rev"] != full_rev:
        raise Fail(f"nixpkgs metadata resolved unexpected repo/rev: {locked}")
    return {key: locked[key] for key in expected}


def update_template_nixpkgs_lock(repo_root: Path, full_rev: str) -> None:
    lock_path = repo_root / "templates/flake.lock"
    data = json_load(lock_path)
    try:
        data["nodes"]["nixpkgs"]["locked"] = nixpkgs_locked_metadata(full_rev)
    except KeyError as error:
        raise Fail(f"{lock_path}: missing nixpkgs lock node") from error
    json_dump(lock_path, data)


def aws_ami_pin_metadata(
    config: TargetConfig, selection: AwsAmiPinSelection, full_rev: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_url": AWS_AMI_INDEX_URL,
        "target": config.name,
        "target_branch": config.branch,
        "host_arches": list(config.host_arches),
        "aws_arches": {arch: aws_ami_arch(arch) for arch in config.host_arches},
        "ami_names": selection.sample_names,
        "rev_prefix": selection.rev_prefix,
        "full_rev": full_rev,
        "preferred_release_prefix": AWS_AMI_PREFERRED_RELEASE_PREFIX,
        "latest_creation_date": selection.latest_creation_date,
        "region_count": len(next(iter(selection.regions_by_arch.values()))),
        "regions_by_arch": {
            arch: list(regions)
            for arch, regions in selection.regions_by_arch.items()
        },
        "candidate_summary": selection.candidate_summary,
        "generated_at": utc_now(),
    }


def assert_aws_ami_pin_metadata(repo_root: Path) -> dict[str, Any]:
    metadata_path = repo_root / AWS_AMI_PIN_METADATA
    if not metadata_path.is_file():
        raise Fail(f"missing AWS AMI pin metadata {AWS_AMI_PIN_METADATA}")
    metadata = json_load(metadata_path)
    full_rev = metadata.get("full_rev")
    rev_prefix = metadata.get("rev_prefix")
    preferred_release_prefix = metadata.get("preferred_release_prefix")
    if not isinstance(full_rev, str) or not isinstance(rev_prefix, str):
        raise Fail("AWS AMI pin metadata must include full_rev and rev_prefix")
    if not is_full_commit_sha(full_rev):
        raise Fail("AWS AMI pin metadata full_rev must be a full 40 character git SHA")
    if len(rev_prefix) != AWS_AMI_REV_PREFIX_LEN or not full_rev.startswith(rev_prefix):
        raise Fail("AWS AMI pin metadata rev_prefix must match full_rev")
    if preferred_release_prefix != AWS_AMI_PREFERRED_RELEASE_PREFIX:
        raise Fail(
            "AWS AMI pin metadata preferred_release_prefix must be "
            f"{AWS_AMI_PREFERRED_RELEASE_PREFIX!r}"
        )
    lock_rev = read_nixpkgs_rev(repo_root / "__missing_branch__", repo_root)
    if lock_rev != full_rev:
        raise Fail(
            "templates/flake.lock nixpkgs rev does not match AWS AMI pin metadata: "
            f"{lock_rev} != {full_rev}"
        )
    ami_names = metadata.get("ami_names")
    if not isinstance(ami_names, dict) or not ami_names:
        raise Fail("AWS AMI pin metadata must include ami_names")
    for arch, name in ami_names.items():
        if not isinstance(arch, str) or not isinstance(name, str):
            raise Fail("AWS AMI pin metadata ami_names must map arch to name")
        match = NIXOS_AMI_NAME_RE.match(name)
        if not match or match.group(1) != rev_prefix:
            raise Fail(f"AWS AMI pin metadata name {name!r} does not match rev prefix {rev_prefix}")
    return metadata


def pin_arch_selection(target: str, arch: str | None) -> str:
    if arch:
        return arch
    if target == "host-service-test":
        return "aarch64"
    return "both"


def placeholder_agent_runtime_store_path(system: str) -> str:
    return f"/nix/store/00000000000000000000000000000000-remote-dev-agent-runtime-{system}"


def agent_runtime_attr(system: str) -> str:
    return f"packages.{system}.remote-dev-agent-runtime"


def agent_runtime_closure_manifest_file(system: str) -> str:
    return f"cloud/agent-runtime-closure-{system}.json"


def host_groups_catalog_file(system: str) -> str:
    return f"cloud/host-groups-catalog-{system}.json"


def host_groups_closure_manifest_file(group_id: str, system: str) -> str:
    return f"{HOST_GROUPS_DIR}/{group_id}-{system}.json"


def host_group_package_attr(group_id: str) -> str:
    return f"remote-dev-host-group-{group_id}"


def host_group_attr(system: str, group_id: str) -> str:
    return f"packages.{system}.{host_group_package_attr(group_id)}"


def host_group_command(command: str) -> dict[str, str]:
    return {"command": command, "relative_path": f"bin/{command}"}


def placeholder_host_groups_store_path(system: str, group_id: str) -> str:
    return f"/nix/store/00000000000000000000000000000000-{host_group_package_attr(group_id)}-{system}"


def agent_runtime_fingerprint(system: str, store_path: str) -> str:
    raw = json.dumps(
        {
            "schema_version": HOST_IMAGE_SPEC_SCHEMA_VERSION,
            "system": system,
            "package_attr": agent_runtime_attr(system),
            "store_path": store_path,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "agent-runtime-v1:" + hashlib.sha256(raw.encode()).hexdigest()


def host_groups_group_fingerprint(group: dict[str, Any], system: str, store_path: str) -> str:
    raw = json.dumps(
        {
            "schema_version": HOST_GROUPS_CATALOG_SCHEMA_VERSION,
            "system": system,
            "id": group["id"],
            "priority": group["priority"],
            "labels": group["labels"],
            "store_path": store_path,
            "commands": [host_group_command(command) for command in group["commands"]],
            "closure_manifest_file": host_groups_closure_manifest_file(group["id"], system),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "host-groups-group-v2:" + hashlib.sha256(raw.encode()).hexdigest()


def host_groups_catalog_groups(
    system: str, group_roots: dict[tuple[str, str], str] | None = None
) -> list[dict[str, Any]]:
    validate_host_group_model()
    groups = []
    for group in HOST_GROUPS:
        group_id = group["id"]
        store_path = (group_roots or {}).get(
            (system, group_id), placeholder_host_groups_store_path(system, group_id)
        )
        groups.append(
            {
                "id": group_id,
                "priority": group["priority"],
                "labels": list(group["labels"]),
                "store_path": store_path,
                "commands": [host_group_command(command) for command in group["commands"]],
                "closure_manifest_file": host_groups_closure_manifest_file(group_id, system),
                "fingerprint": host_groups_group_fingerprint(group, system, store_path),
            }
        )
    return groups


def host_groups_catalog_fingerprint(system: str, groups: list[dict[str, Any]]) -> str:
    raw = json.dumps(
        {
            "schema_version": HOST_GROUPS_CATALOG_SCHEMA_VERSION,
            "system": system,
            "groups": groups,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "host-groups-catalog-v2:" + hashlib.sha256(raw.encode()).hexdigest()


def write_host_groups_catalogs(
    branch_dir: Path,
    config: TargetConfig,
    group_roots: dict[tuple[str, str], str] | None = None,
) -> dict[str, dict[str, str]]:
    catalogs: dict[str, dict[str, str]] = {}
    for arch in config.host_arches:
        system = f"{arch}-linux"
        groups = host_groups_catalog_groups(system, group_roots)
        fingerprint = host_groups_catalog_fingerprint(system, groups)
        catalog_file = host_groups_catalog_file(system)
        json_dump(
            branch_dir / catalog_file,
            {
                "schema_version": HOST_GROUPS_CATALOG_SCHEMA_VERSION,
                "system": system,
                "fingerprint": fingerprint,
                "groups": groups,
            },
        )
        for group in groups:
            group_id = group["id"]
            manifest_path = branch_dir / group["closure_manifest_file"]
            if not manifest_path.is_file():
                json_dump(
                    manifest_path,
                    {
                        "schema_version": 1,
                        "system": system,
                        "group": group_id,
                        "package_attr": host_group_attr(system, group_id),
                        "fingerprint": group["fingerprint"],
                        "store_path": group["store_path"],
                        "paths": [group["store_path"]],
                    },
                )
        catalogs[system] = {"file": catalog_file, "fingerprint": fingerprint}
    return catalogs


def write_host_specs(
    branch_dir: Path,
    config: TargetConfig,
    nixpkgs_rev: str,
    trusted_public_key: str,
    host_groups_catalogs: dict[str, dict[str, str]],
    agent_runtimes: dict[str, str] | None = None,
) -> None:
    for arch in config.host_arches:
        system = f"{arch}-linux"
        agent_runtime_store_path = (agent_runtimes or {}).get(
            system, placeholder_agent_runtime_store_path(system)
        )
        agent_runtime_closure_manifest = agent_runtime_closure_manifest_file(system)
        host_groups_catalog = host_groups_catalogs[system]
        catalog = json_load(branch_dir / host_groups_catalog["file"])
        git_core_group = next(
            (group for group in catalog.get("groups", []) if group.get("id") == "git-core"),
            None,
        )
        if not isinstance(git_core_group, dict):
            raise Fail(f"{host_groups_catalog['file']}: missing git-core group")
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
                "preferred_release_prefix": AWS_AMI_PREFERRED_RELEASE_PREFIX,
                "arch": aws_ami_arch(arch),
                "root_device_name": "/dev/xvda",
            },
            "bootstrap": {
                "agent_runtime_attr": agent_runtime_attr(system),
                "nixpkgs_rev": nixpkgs_rev,
                "system": system,
                "agent_runtime_store_path": agent_runtime_store_path,
                "agent_runtime_closure_manifest_file": agent_runtime_closure_manifest,
                "agent_runtime_fingerprint": agent_runtime_fingerprint(
                    system, agent_runtime_store_path
                ),
                "host_groups_catalog_file": host_groups_catalog["file"],
                "host_groups_catalog_fingerprint": host_groups_catalog["fingerprint"],
                "git_core_group_store_path": git_core_group["store_path"],
                "git_core_group_fingerprint": git_core_group["fingerprint"],
            },
            "nix_cache": {
                "substituter_url": f"https://raw.githubusercontent.com/{REPO}/{config.branch}/{NIX_CACHE_DIR}",
                "trusted_public_key": trusted_public_key,
            },
        }
        json_dump(branch_dir / HOST_IMAGE_SPEC_DIR / f"{arch}.json", spec)
        closure_path = branch_dir / agent_runtime_closure_manifest
        if not closure_path.is_file():
            closure = {
                "schema_version": 1,
                "system": system,
                "agent_runtime_attr": agent_runtime_attr(system),
                "agent_runtime_store_path": agent_runtime_store_path,
                "paths": [agent_runtime_store_path],
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


def nix_build_store_path(branch_dir: Path, system: str, attr: str) -> str:
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
    return output.splitlines()[-1]


def nix_path_info_recursive(branch_dir: Path, store_path: str) -> Any:
    return json.loads(
        run(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command",
                "path-info",
                "--json",
                "--sigs",
                "--size",
                "--recursive",
                store_path,
            ],
            cwd=branch_dir,
            capture=True,
        )
    )


def maybe_realize_runtime_and_cache(
    branch_dir: Path, config: TargetConfig, signing_key: str | None, trusted_public_key: str
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    if not signing_key:
        return {}, {}
    agent_runtimes: dict[str, str] = {}
    group_roots: dict[tuple[str, str], str] = {}
    cache_roots: list[str] = []
    for arch in config.host_arches:
        system = f"{arch}-linux"
        attr = f"path:{branch_dir}#{agent_runtime_attr(system)}"
        output = nix_build_store_path(branch_dir, system, attr)
        agent_runtimes[system] = output
        cache_roots.append(output)
        json_dump(
            branch_dir / agent_runtime_closure_manifest_file(system),
            {
                "schema_version": 1,
                "system": system,
                "agent_runtime_attr": agent_runtime_attr(system),
                "agent_runtime_store_path": output,
                "paths": nix_path_info_recursive(branch_dir, output),
            },
        )
        for group in HOST_GROUPS:
            group_id = group["id"]
            output = nix_build_store_path(branch_dir, system, f"path:{branch_dir}#{host_group_attr(system, group_id)}")
            group_roots[(system, group_id)] = output
            cache_roots.append(output)
            fingerprint = host_groups_group_fingerprint(group, system, output)
            json_dump(
                branch_dir / host_groups_closure_manifest_file(group_id, system),
                {
                    "schema_version": 1,
                    "system": system,
                    "group": group_id,
                    "package_attr": host_group_attr(system, group_id),
                    "fingerprint": fingerprint,
                    "store_path": output,
                    "paths": nix_path_info_recursive(branch_dir, output),
                },
            )
    write_signed_nix_cache(branch_dir, signing_key, trusted_public_key, cache_roots)
    return agent_runtimes, group_roots


def write_signed_nix_cache(
    branch_dir: Path, signing_key: str, trusted_public_key: str, runtime_roots: list[str]
) -> None:
    roots = sorted(remote_dev_bin_cache_roots(branch_dir, runtime_roots))
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


def audit_runtime_closures(branch_dir: Path, config: TargetConfig) -> list[dict[str, Any]]:
    audits = []
    for arch in config.host_arches:
        system = f"{arch}-linux"
        closure_path = branch_dir / agent_runtime_closure_manifest_file(system)
        audit = audit_closure_manifest(branch_dir / NIX_CACHE_DIR, json_load(closure_path))
        audit["system"] = system
        if audit["closure_paths"] > MAX_AGENT_RUNTIME_CLOSURE_PATHS:
            raise Fail(
                "agent runtime closure is not minimal: "
                f"paths={audit['closure_paths']} max={MAX_AGENT_RUNTIME_CLOSURE_PATHS}"
            )
        assert_agent_runtime_closure_names(audit["path_names"])
        audits.append(audit)
        marker_report = " ".join(
            f"{marker}={'present' if audit['marker_presence'][marker] else 'absent'}"
            for marker in CLOSURE_AUDIT_MARKERS
        )
        top_nar_report = ";".join(
            f"{entry['nar_size']}:{entry['path']}"
            for entry in audit["top_nar_size_paths"]
        )
        print(
            "closure audit "
            f"system={system} "
            f"paths={audit['closure_paths']} "
            f"unsigned={audit['unsigned_paths']} "
            f"cache_compressed_bytes={audit['cache_compressed_bytes']} "
            f"{marker_report} "
            f"top_nar_size={top_nar_report}"
        )
    return audits


def audit_closure_manifest(cache_dir: Path, closure: dict[str, Any]) -> dict[str, Any]:
    paths = iter_nix_path_info(closure.get("paths"))
    missing_narinfo = []
    large_sources = []
    unsigned_count = 0
    marker_presence = dict.fromkeys(CLOSURE_AUDIT_MARKERS, False)
    top_nar_size_paths = []
    path_names = []
    for path, info in paths:
        name = store_path_name(path)
        path_names.append(name)
        nar_size = nix_path_size(info)
        top_nar_size_paths.append({"path": path, "nar_size": nar_size})
        for marker in CLOSURE_AUDIT_MARKERS:
            if marker in name:
                marker_presence[marker] = True
        if large_source_path(path, info):
            large_sources.append(f"{path} ({nix_path_size(info)} bytes)")
        if not cache_nixos_signed_path(info):
            unsigned_count += 1
            if not narinfo_path(cache_dir, path).is_file():
                missing_narinfo.append(path)
    if large_sources:
        raise Fail(
            "runtime closure contains source paths over "
            f"{BOOTSTRAP_SOURCE_MAX_BYTES} bytes: {', '.join(large_sources)}"
        )
    if missing_narinfo:
        raise Fail(
            "unsigned runtime closure paths missing remote-dev-bin narinfo: "
            + ", ".join(missing_narinfo)
        )
    top_nar_size_paths.sort(key=lambda entry: entry["nar_size"], reverse=True)
    return {
        "closure_paths": len(paths),
        "unsigned_paths": unsigned_count,
        "cache_compressed_bytes": compressed_cache_size(cache_dir),
        "top_nar_size_paths": top_nar_size_paths[:CLOSURE_AUDIT_TOP_NAR_PATHS],
        "marker_presence": marker_presence,
        "path_names": path_names,
    }


def assert_agent_runtime_closure_names(path_names: list[str]) -> None:
    unexpected = []
    denied = []
    for raw_name in path_names:
        _, sep, name = raw_name.partition("-")
        if not sep:
            unexpected.append(raw_name)
            continue
        if not any(
            name == allowed or name.startswith(allowed)
            for allowed in AGENT_RUNTIME_CLOSURE_ALLOWED_NAMES
        ):
            unexpected.append(raw_name)
        if any(marker in name for marker in AGENT_RUNTIME_CLOSURE_DENIED_MARKERS):
            denied.append(raw_name)
    if unexpected:
        raise Fail("agent runtime closure contains non-agent paths: " + ", ".join(unexpected))
    if denied:
        raise Fail("agent runtime closure contains forbidden toolchain paths: " + ", ".join(denied))


def large_source_path(path: str, info: dict[str, Any]) -> bool:
    _, _, name = store_path_name(path).partition("-")
    return name.endswith("-source") and nix_path_size(info) > BOOTSTRAP_SOURCE_MAX_BYTES


def nix_path_size(info: dict[str, Any]) -> int:
    for key in ("narSize", "nar_size", "size"):
        value = info.get(key)
        if isinstance(value, int):
            return value
    return 0


def narinfo_path(cache_dir: Path, store_path: str) -> Path:
    store_hash = store_path_name(store_path).split("-", 1)[0]
    return cache_dir / f"{store_hash}.narinfo"


def compressed_cache_size(cache_dir: Path) -> int:
    nar_dir = cache_dir / "nar"
    if not nar_dir.is_dir():
        return 0
    return sum(path.stat().st_size for path in nar_dir.glob("*.nar.xz") if path.is_file())


def remote_dev_bin_cache_roots(branch_dir: Path, runtime_roots: list[str]) -> set[str]:
    roots = set(runtime_roots)
    for runtime_root in runtime_roots:
        output = run(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command",
                "path-info",
                "--json",
                "--sigs",
                "--size",
                "--recursive",
                runtime_root,
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


def is_placeholder_store_path(path: str) -> bool:
    return store_path_name(path).split("-", 1)[0] == "0" * 32


def closure_cache_path_refs(label: str, closure: dict[str, Any]) -> list[CachePathRef]:
    if "paths" not in closure:
        raise Fail(f"{label}: missing paths")
    return [
        CachePathRef(f"{label}: paths", path, info, False)
        for path, info in iter_nix_path_info(closure["paths"])
    ]


def collect_branch_cache_path_refs(branch_dir: Path, config: TargetConfig) -> list[CachePathRef]:
    refs: list[CachePathRef] = []
    for arch in config.host_arches:
        system = f"{arch}-linux"
        spec_path = branch_dir / HOST_IMAGE_SPEC_DIR / f"{arch}.json"
        if not spec_path.is_file():
            raise Fail(f"missing host runtime spec {spec_path.relative_to(branch_dir)}")
        spec = json_load(spec_path)
        bootstrap = spec.get("bootstrap")
        if not isinstance(bootstrap, dict):
            raise Fail(f"{spec_path}: bootstrap must be an object")

        agent_runtime_store_path = bootstrap.get("agent_runtime_store_path")
        if not isinstance(agent_runtime_store_path, str):
            raise Fail(f"{spec_path}: bootstrap.agent_runtime_store_path is missing")
        refs.append(
            CachePathRef(
                f"{spec_path.relative_to(branch_dir)}: bootstrap.agent_runtime_store_path",
                agent_runtime_store_path,
                None,
                True,
            )
        )

        agent_closure_file = bootstrap.get("agent_runtime_closure_manifest_file")
        if not isinstance(agent_closure_file, str) or not agent_closure_file:
            raise Fail(f"{spec_path}: bootstrap.agent_runtime_closure_manifest_file is missing")
        agent_closure_path = branch_dir / agent_closure_file
        if not agent_closure_path.is_file():
            raise Fail(f"{spec_path}: agent runtime closure manifest is missing")
        refs.extend(
            closure_cache_path_refs(
                str(Path(agent_closure_file)),
                json_load(agent_closure_path),
            )
        )

        catalog_file = bootstrap.get("host_groups_catalog_file")
        if catalog_file != host_groups_catalog_file(system):
            raise Fail(f"{spec_path}: host groups catalog file mismatch")
        catalog_path = branch_dir / catalog_file
        if not catalog_path.is_file():
            raise Fail(f"{spec_path}: host groups catalog is missing")
        catalog = json_load(catalog_path)
        groups = catalog.get("groups")
        if not isinstance(groups, list) or not groups:
            raise Fail(f"{catalog_file}: groups must be a non-empty list")
        for group in groups:
            if not isinstance(group, dict):
                raise Fail(f"{catalog_file}: host groups group must be an object")
            group_id = group.get("id")
            if not isinstance(group_id, str) or not group_id:
                raise Fail(f"{catalog_file}: host groups group is missing id")
            store_path = group.get("store_path")
            if not isinstance(store_path, str):
                raise Fail(f"{catalog_file}: group {group_id} store_path must be a string")
            refs.append(
                CachePathRef(
                    f"{catalog_file}: group {group_id} store_path",
                    store_path,
                    None,
                    True,
                )
            )
            closure_manifest_file = group.get("closure_manifest_file")
            if closure_manifest_file != host_groups_closure_manifest_file(group_id, system):
                raise Fail(f"{catalog_file}: group {group_id} closure manifest mismatch")
            closure_manifest_path = branch_dir / closure_manifest_file
            if not closure_manifest_path.is_file():
                raise Fail(f"{catalog_file}: group {group_id} closure manifest is missing")
            refs.extend(
                closure_cache_path_refs(
                    str(Path(closure_manifest_file)),
                    json_load(closure_manifest_path),
                )
            )
    return refs


def cache_ref_needs_local_narinfo(ref: CachePathRef) -> bool:
    store_path_name(ref.path)
    if is_placeholder_store_path(ref.path):
        return False
    if ref.local_required:
        return True
    return ref.info is None or not cache_nixos_signed_path(ref.info)


def parse_narinfo_fields(text: str, label: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            raise Fail(f"{label}: invalid narinfo line {line!r}")
        key = key.strip()
        value = value.strip()
        if not key:
            raise Fail(f"{label}: invalid empty narinfo field")
        if key == "Sig":
            continue
        if key in fields:
            raise Fail(f"{label}: duplicate narinfo field {key}")
        fields[key] = value
    return fields


def require_narinfo_relative_url(fields: dict[str, str], label: str) -> str:
    url = fields.get("URL")
    if not url:
        raise Fail(f"{label}: narinfo is missing URL")
    if "://" in url or url.startswith("/"):
        raise Fail(f"{label}: narinfo URL must be relative, got {url}")
    parts = Path(url).parts
    if not parts or parts[0] != "nar" or ".." in parts:
        raise Fail(f"{label}: narinfo URL must stay under nar/, got {url}")
    return url


def required_remote_cache_entries(branch_dir: Path, config: TargetConfig) -> list[RemoteCacheEntry]:
    cache_dir = branch_dir / NIX_CACHE_DIR
    entries: dict[str, RemoteCacheEntry] = {}
    missing: list[str] = []
    for ref in collect_branch_cache_path_refs(branch_dir, config):
        if not cache_ref_needs_local_narinfo(ref):
            continue
        info_path = narinfo_path(cache_dir, ref.path)
        if not info_path.is_file():
            missing.append(f"{ref.label} {ref.path}")
            continue
        text = info_path.read_text()
        fields = parse_narinfo_fields(text, str(info_path.relative_to(branch_dir)))
        if fields.get("StorePath") != ref.path:
            raise Fail(
                f"{info_path.relative_to(branch_dir)}: StorePath {fields.get('StorePath')!r} "
                f"does not match required path {ref.path}"
            )
        nar_relative = require_narinfo_relative_url(
            fields,
            str(info_path.relative_to(branch_dir)),
        )
        nar_path = cache_dir / nar_relative
        if not nar_path.is_file():
            raise Fail(
                f"{info_path.relative_to(branch_dir)}: referenced Nar file is missing: "
                f"{Path(NIX_CACHE_DIR) / nar_relative}"
            )
        entry = RemoteCacheEntry(
            label=ref.label,
            store_path=ref.path,
            narinfo_path=info_path,
            narinfo_relative=str(Path(NIX_CACHE_DIR) / info_path.name),
            nar_relative=nar_relative,
        )
        previous = entries.get(ref.path)
        if previous is not None:
            if previous.narinfo_relative != entry.narinfo_relative or previous.nar_relative != entry.nar_relative:
                raise Fail(f"{ref.path}: cache metadata is inconsistent across references")
            continue
        entries[ref.path] = entry
    if missing:
        raise Fail("required branch cache paths are missing generated narinfo: " + ", ".join(missing))
    return sorted(entries.values(), key=lambda entry: entry.store_path)


def validate_cached_store_path(branch_dir: Path, label: str, path: str) -> None:
    store_path_name(path)
    if is_placeholder_store_path(path):
        return
    if not narinfo_path(branch_dir / NIX_CACHE_DIR, path).is_file():
        raise Fail(f"{label} {path} is missing from generated Nix cache")


def raw_github_artifact_url(artifact_sha: str, relative: str) -> str:
    if not is_full_commit_sha(artifact_sha):
        raise Fail(f"artifact SHA must be a full 40 character git SHA, got {artifact_sha}")
    quoted = urllib.parse.quote(relative, safe="/")
    return f"https://raw.githubusercontent.com/{REPO}/{artifact_sha}/{quoted}"


def fetch_remote_bytes(
    url: str,
    label: str,
    max_bytes: int,
    attempts: int,
    sleep_secs: float,
    headers: dict[str, str] | None = None,
    method: str = "GET",
) -> bytes:
    if attempts < 1:
        raise Fail("remote cache verification attempts must be at least 1")
    if sleep_secs < 0:
        raise Fail("remote cache verification sleep seconds must not be negative")
    request_headers = {"User-Agent": "remote-dev-bin-publish"}
    if headers:
        request_headers.update(headers)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if max_bytes == 0:
                    return b""
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise Fail(f"{label}: remote response is larger than {max_bytes} bytes")
                return data
        except (OSError, urllib.error.URLError, Fail) as error:
            last_error = error
            if attempt < attempts:
                time.sleep(sleep_secs)
    raise Fail(f"{label}: fetch {url}: {last_error}")


def verify_remote_cache_entry(
    artifact_sha: str,
    branch_dir: Path,
    entry: RemoteCacheEntry,
    attempts: int,
    sleep_secs: float,
) -> None:
    narinfo_url = raw_github_artifact_url(artifact_sha, entry.narinfo_relative)
    remote_text = fetch_remote_bytes(
        narinfo_url,
        entry.narinfo_relative,
        REMOTE_CACHE_NARINFO_MAX_BYTES,
        attempts,
        sleep_secs,
    ).decode("utf-8")
    local_text = entry.narinfo_path.read_text()
    if remote_text != local_text:
        raise Fail(f"{entry.narinfo_relative}: remote narinfo content does not match generated tree")
    fields = parse_narinfo_fields(remote_text, entry.narinfo_relative)
    if fields.get("StorePath") != entry.store_path:
        raise Fail(f"{entry.narinfo_relative}: remote StorePath does not match {entry.store_path}")
    nar_relative = require_narinfo_relative_url(fields, entry.narinfo_relative)
    if nar_relative != entry.nar_relative:
        raise Fail(f"{entry.narinfo_relative}: remote Nar URL does not match generated tree")
    local_nar = branch_dir / NIX_CACHE_DIR / entry.nar_relative
    if not local_nar.is_file():
        raise Fail(f"{entry.narinfo_relative}: local Nar file is missing before remote verification")
    nar_label = str(Path(NIX_CACHE_DIR) / entry.nar_relative)
    nar_url = raw_github_artifact_url(artifact_sha, nar_label)
    try:
        fetch_remote_bytes(nar_url, nar_label, 0, attempts, sleep_secs, method="HEAD")
    except Fail as error:
        if "HTTP Error 405" not in str(error):
            raise
        fetch_remote_bytes(
            nar_url,
            nar_label,
            1,
            attempts,
            sleep_secs,
            headers={"Range": "bytes=0-0"},
        )


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
    write_nix_cache_info(branch_dir, trusted_key)
    agent_runtimes, group_roots = maybe_realize_runtime_and_cache(
        branch_dir, config, args.nix_cache_signing_key_file, trusted_key
    )
    host_groups_catalogs = write_host_groups_catalogs(branch_dir, config, group_roots)
    write_host_specs(
        branch_dir,
        config,
        nixpkgs_rev,
        trusted_key,
        host_groups_catalogs,
        agent_runtimes,
    )
    closure_audits = (
        audit_runtime_closures(branch_dir, config)
        if args.nix_cache_signing_key_file
        else []
    )
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
        "host_groups_catalogs": {
            system: catalog["file"] for system, catalog in sorted(host_groups_catalogs.items())
        },
        "artifacts": copied,
        "host_service_image": "cloud/host-service-image.json"
        if (branch_dir / "cloud/host-service-image.json").is_file()
        else None,
        "closure_audit": closure_audits,
        "retention": {
            "max_commits": config.retention_commits,
            "max_days": config.retention_days,
        },
        "generated_at": utc_now(),
    }
    json_dump(branch_dir / "build-manifest.json", aggregate)
    validate_tree(branch_dir, config)


def validate_tree(branch_dir: Path, config: TargetConfig) -> None:
    validate_host_group_model()
    for relative in [
        "build-manifest.json",
        "flake.nix",
        "flake.lock",
        f"{NIX_CACHE_DIR}/nix-cache-info",
    ]:
        if not (branch_dir / relative).is_file():
            raise Fail(f"missing required branch artifact {relative}")
    for stale in [
        AWS_BOOTSTRAP_FLAKE,
        AWS_BOOTSTRAP_LOCK,
        "cloud/aws-bootstrap-closure-x86_64-linux.json",
        "cloud/aws-bootstrap-closure-aarch64-linux.json",
    ]:
        if (branch_dir / stale).exists():
            raise Fail(f"stale bootstrap artifact must not be rendered: {stale}")
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
        system = f"{arch}-linux"
        spec_path = branch_dir / HOST_IMAGE_SPEC_DIR / f"{arch}.json"
        if not spec_path.is_file():
            raise Fail(f"missing host runtime spec {spec_path.relative_to(branch_dir)}")
        spec = json_load(spec_path)
        if spec.get("arch") != arch:
            raise Fail(f"{spec_path}: arch mismatch")
        if spec.get("schema_version") != HOST_IMAGE_SPEC_SCHEMA_VERSION:
            raise Fail(f"{spec_path}: schema_version mismatch")
        closure = branch_dir / spec["bootstrap"]["agent_runtime_closure_manifest_file"]
        if not closure.is_file():
            raise Fail(f"{spec_path}: closure manifest is missing")
        closure_manifest = json_load(closure)
        agent_runtime_store_path = spec["bootstrap"].get("agent_runtime_store_path")
        if not isinstance(agent_runtime_store_path, str):
            raise Fail(f"{spec_path}: bootstrap.agent_runtime_store_path is missing")
        if closure_manifest.get("agent_runtime_store_path") != agent_runtime_store_path:
            raise Fail(f"{closure.relative_to(branch_dir)}: agent_runtime_store_path mismatch")
        aws_ami = spec.get("aws_official_nixos_ami")
        if not isinstance(aws_ami, dict):
            raise Fail(f"{spec_path}: aws_official_nixos_ami is missing")
        if aws_ami.get("preferred_release_prefix") != AWS_AMI_PREFERRED_RELEASE_PREFIX:
            raise Fail(
                f"{spec_path}: aws_official_nixos_ami.preferred_release_prefix must be "
                f"{AWS_AMI_PREFERRED_RELEASE_PREFIX!r}"
            )
        validate_cached_store_path(
            branch_dir, f"{spec_path}: agent_runtime_store_path", agent_runtime_store_path
        )
        expected_agent_fingerprint = agent_runtime_fingerprint(system, agent_runtime_store_path)
        if spec["bootstrap"].get("agent_runtime_fingerprint") != expected_agent_fingerprint:
            raise Fail(f"{spec_path}: agent runtime fingerprint mismatch")
        catalog_file = spec["bootstrap"].get("host_groups_catalog_file")
        catalog_fingerprint = spec["bootstrap"].get("host_groups_catalog_fingerprint")
        expected_catalog_file = host_groups_catalog_file(system)
        if catalog_file != expected_catalog_file:
            raise Fail(f"{spec_path}: host groups catalog file mismatch")
        catalog_path = branch_dir / catalog_file
        if not catalog_path.is_file():
            raise Fail(f"{spec_path}: host groups catalog is missing")
        catalog = json_load(catalog_path)
        groups = catalog.get("groups")
        if catalog.get("schema_version") != HOST_GROUPS_CATALOG_SCHEMA_VERSION:
            raise Fail(f"{catalog_file}: schema_version mismatch")
        if catalog.get("system") != system:
            raise Fail(f"{catalog_file}: system mismatch")
        if not isinstance(groups, list) or not groups:
            raise Fail(f"{catalog_file}: groups must be a non-empty list")
        if catalog.get("fingerprint") != catalog_fingerprint:
            raise Fail(f"{spec_path}: host groups catalog fingerprint mismatch")
        expected_fingerprint = host_groups_catalog_fingerprint(system, groups)
        if catalog_fingerprint != expected_fingerprint:
            raise Fail(f"{catalog_file}: fingerprint does not match catalog content")
        expected_group_specs = {group["id"]: group for group in HOST_GROUPS}
        seen_groups: set[str] = set()
        seen_commands: set[str] = set()
        for group in groups:
            if not isinstance(group, dict):
                raise Fail(f"{catalog_file}: host groups group must be an object")
            group_id = group.get("id")
            if not isinstance(group_id, str) or not group_id:
                raise Fail(f"{catalog_file}: host groups group is missing id")
            if group_id in seen_groups:
                raise Fail(f"{catalog_file}: duplicate host groups group {group_id}")
            seen_groups.add(group_id)
            if group_id not in expected_group_specs:
                raise Fail(f"{catalog_file}: unexpected host groups group {group_id}")
            if group_id == "git-core":
                if spec["bootstrap"].get("git_core_group_store_path") != group.get("store_path"):
                    raise Fail(f"{spec_path}: git-core store path does not match catalog")
                if spec["bootstrap"].get("git_core_group_fingerprint") != group.get("fingerprint"):
                    raise Fail(f"{spec_path}: git-core fingerprint does not match catalog")
                validate_cached_store_path(
                    branch_dir,
                    f"{spec_path}: git_core_group_store_path",
                    group["store_path"],
                )
            if "scope" in group or "policy" in group:
                raise Fail(f"{catalog_file}: group {group_id} must not include shell scope or policy")
            priority = group.get("priority")
            if not isinstance(priority, int) or priority < 0:
                raise Fail(f"{catalog_file}: group {group_id} priority must be a non-negative integer")
            if "installable" in group:
                raise Fail(f"{catalog_file}: group {group_id} must not include installable")
            store_path = group.get("store_path")
            if not isinstance(store_path, str) or not store_path.startswith("/nix/store/"):
                raise Fail(f"{catalog_file}: group {group_id} store_path must be a /nix/store path")
            closure_manifest_file = group.get("closure_manifest_file")
            if closure_manifest_file != host_groups_closure_manifest_file(group_id, system):
                raise Fail(f"{catalog_file}: group {group_id} closure manifest mismatch")
            closure_manifest_path = branch_dir / closure_manifest_file
            if not closure_manifest_path.is_file():
                raise Fail(f"{catalog_file}: group {group_id} closure manifest is missing")
            closure_manifest = json_load(closure_manifest_path)
            if "installable" in closure_manifest:
                raise Fail(f"{closure_manifest_file}: must not include installable")
            if closure_manifest.get("package_attr") != host_group_attr(system, group_id):
                raise Fail(f"{closure_manifest_file}: package_attr mismatch")
            if closure_manifest.get("store_path") != store_path:
                raise Fail(f"{closure_manifest_file}: store_path mismatch")
            commands = group.get("commands")
            if not isinstance(commands, list):
                raise Fail(f"{catalog_file}: group {group_id} commands must be a list")
            expected_commands = [host_group_command(command) for command in expected_group_specs[group_id]["commands"]]
            if commands != expected_commands:
                raise Fail(f"{catalog_file}: group {group_id} commands mismatch")
            for command_entry in commands:
                command = command_entry.get("command") if isinstance(command_entry, dict) else None
                relative_path = command_entry.get("relative_path") if isinstance(command_entry, dict) else None
                if not isinstance(command, str) or not command:
                    raise Fail(f"{catalog_file}: group {group_id} has invalid command")
                if not isinstance(relative_path, str) or not relative_path or relative_path.startswith("/"):
                    raise Fail(f"{catalog_file}: group {group_id} command {command} has invalid relative_path")
                if ".." in Path(relative_path).parts:
                    raise Fail(f"{catalog_file}: group {group_id} command {command} relative_path escapes bundle")
                if command in seen_commands:
                    raise Fail(f"{catalog_file}: duplicate host groups command {command}")
                seen_commands.add(command)
        expected_groups = {group["id"] for group in HOST_GROUPS}
        if seen_groups != expected_groups:
            raise Fail(f"{catalog_file}: host groups groups mismatch")
    missing_arches = {"x86_64", "aarch64"} - set(config.host_arches)
    for arch in missing_arches:
        if (branch_dir / HOST_IMAGE_SPEC_DIR / f"{arch}.json").exists():
            raise Fail(f"unexpected host runtime spec for unbuilt arch {arch}")
        system = f"{arch}-linux"
        if (branch_dir / host_groups_catalog_file(system)).exists():
            raise Fail(f"unexpected host groups catalog for unbuilt arch {arch}")
    required_remote_cache_entries(branch_dir, config)


def cmd_validate_tree(args: argparse.Namespace) -> None:
    validate_tree(Path(args.branch_dir), target_config(args.target, args.arch))


def cmd_verify_remote_cache(args: argparse.Namespace) -> None:
    branch_dir = Path(args.branch_dir)
    config = target_config(args.target, args.arch)
    entries = required_remote_cache_entries(branch_dir, config)
    if not entries:
        raise Fail(
            "remote cache verification found no concrete cached paths; "
            "render-tree likely ran without a signing key"
        )
    for entry in entries:
        verify_remote_cache_entry(
            args.artifact_sha,
            branch_dir,
            entry,
            args.attempts,
            args.sleep_secs,
        )
    print(
        "remote cache verified "
        f"target={config.name} "
        f"arch={','.join(config.host_arches)} "
        f"artifact_sha={args.artifact_sha} "
        f"entries={len(entries)}"
    )


def cmd_refresh_aws_ami_pin(args: argparse.Namespace) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config = target_config(args.target, pin_arch_selection(args.target, args.arch))
    index = read_aws_ami_index(args.images_json)
    selection = select_aws_ami_pin(index, config.host_arches)
    full_rev = resolve_nixpkgs_full_rev(selection.rev_prefix)
    update_template_nixpkgs_lock(repo_root, full_rev)
    metadata = aws_ami_pin_metadata(config, selection, full_rev)
    json_dump(repo_root / AWS_AMI_PIN_METADATA, metadata)
    assert_aws_ami_pin_metadata(repo_root)
    print(
        "AWS AMI pin refreshed "
        f"target={config.name} "
        f"arch={','.join(config.host_arches)} "
        f"prefix={selection.rev_prefix} "
        f"full_rev={full_rev} "
        f"regions={metadata['region_count']}"
    )


def cmd_verify_aws_ami_pin(args: argparse.Namespace) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    metadata = assert_aws_ami_pin_metadata(repo_root)
    config = target_config(args.target, pin_arch_selection(args.target, args.arch))
    index = read_aws_ami_index(args.images_json)
    selection = select_aws_ami_pin(index, config.host_arches)
    if selection.rev_prefix != metadata["rev_prefix"]:
        raise Fail(
            "AWS AMI pin metadata is stale for current full-coverage official AMI index: "
            f"{metadata['rev_prefix']} != {selection.rev_prefix}"
        )
    print(
        "AWS AMI pin verified "
        f"target={config.name} "
        f"arch={','.join(config.host_arches)} "
        f"prefix={selection.rev_prefix} "
        f"full_rev={metadata['full_rev']}"
    )


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


def require_non_empty_arg(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Fail(f"{name} must be a non-empty string")
    return value.strip()


def decode_gcloud_json(text: str, context: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise Fail(f"{context}: gcloud did not return JSON: {error}") from error


def read_gcloud_json(args: list[str], context: str) -> Any:
    return decode_gcloud_json(run(args, capture=True), context)


def cloud_run_revision_name_from_ref(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip().rstrip("/")
    if not trimmed:
        return None
    return trimmed.rsplit("/", 1)[-1]


def parse_cloud_run_rfc3339(value: Any, context: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise Fail(f"{context}: missing creation timestamp")
    match = CLOUD_RUN_RFC3339_RE.match(value)
    if not match:
        raise Fail(f"{context}: invalid RFC3339 timestamp {value!r}")
    fraction = match.group("fraction")
    microseconds = ""
    if fraction is not None:
        microseconds = "." + fraction[:6].ljust(6, "0")
    offset = "+00:00" if match.group("offset") == "Z" else match.group("offset")
    normalized = f"{match.group('datetime')}{microseconds}{offset}"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise Fail(f"{context}: invalid RFC3339 timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise Fail(f"{context}: timestamp must include a timezone")
    return parsed.astimezone(dt.UTC)


def parse_cloud_run_revisions(value: Any) -> tuple[CloudRunRevision, ...]:
    if not isinstance(value, list):
        raise Fail("Cloud Run revisions list JSON must be a list")
    revisions: list[CloudRunRevision] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        context = f"Cloud Run revision[{index}]"
        if not isinstance(raw, dict):
            raise Fail(f"{context}: must be an object")
        metadata = raw.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise Fail(f"{context}: metadata must be an object")
        metadata_obj = metadata if isinstance(metadata, dict) else {}
        name = cloud_run_revision_name_from_ref(metadata_obj.get("name"))
        if name is None:
            name = cloud_run_revision_name_from_ref(raw.get("name"))
        if name is None:
            raise Fail(f"{context}: missing revision name")
        if name in seen:
            raise Fail(f"duplicate Cloud Run revision name {name}")
        seen.add(name)
        timestamp = metadata_obj.get("creationTimestamp", raw.get("createTime"))
        created_at = parse_cloud_run_rfc3339(timestamp, context)
        revisions.append(
            CloudRunRevision(
                name=name,
                created_at=created_at,
                created_at_raw=timestamp,
            )
        )
    return tuple(sorted(revisions, key=lambda item: (item.created_at, item.name), reverse=True))


def add_protected_revision(
    protected: dict[str, set[str]],
    name: str | None,
    reason: str,
) -> None:
    if name is None:
        return
    protected.setdefault(name, set()).add(reason)


def first_revision_ref(
    sources: tuple[tuple[str, dict[str, Any]], ...],
    fields: tuple[str, ...],
) -> tuple[str, str] | None:
    for source_name, source in sources:
        for field in fields:
            name = cloud_run_revision_name_from_ref(source.get(field))
            if name is not None:
                return name, f"{source_name}.{field}"
    return None


def protect_cloud_run_traffic_revisions(
    protected: dict[str, set[str]],
    label: str,
    targets: Any,
) -> None:
    if targets is None:
        return
    if not isinstance(targets, list):
        raise Fail(f"Cloud Run service describe JSON {label} must be a list")
    for index, target in enumerate(targets):
        context = f"{label}[{index}]"
        if not isinstance(target, dict):
            raise Fail(f"Cloud Run service describe JSON {context} must be an object")
        revision = None
        for field in CLOUD_RUN_TRAFFIC_REVISION_FIELDS:
            revision = cloud_run_revision_name_from_ref(target.get(field))
            if revision is not None:
                break
        if revision is None:
            continue
        has_tag = isinstance(target.get("tag"), str) and bool(target["tag"])
        percent = target.get("percent", target.get("trafficPercent"))
        has_traffic = isinstance(percent, int | float) and percent > 0
        if has_tag and has_traffic:
            reason = "traffic/tag"
        elif has_tag:
            reason = "tag"
        elif has_traffic:
            reason = "traffic"
        else:
            reason = "traffic reference"
        add_protected_revision(protected, revision, f"{context} {reason}")


def cloud_run_service_protected_revisions(service: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(service, dict):
        raise Fail("Cloud Run service describe JSON must be an object")
    status = service.get("status")
    if status is not None and not isinstance(status, dict):
        raise Fail("Cloud Run service describe JSON status must be an object")
    spec = service.get("spec")
    if spec is not None and not isinstance(spec, dict):
        raise Fail("Cloud Run service describe JSON spec must be an object")
    status_obj = status if isinstance(status, dict) else {}
    spec_obj = spec if isinstance(spec, dict) else {}
    sources = (("status", status_obj), ("service", service))

    protected: dict[str, set[str]] = {}
    latest_ready = first_revision_ref(sources, CLOUD_RUN_LATEST_READY_FIELDS)
    if latest_ready is None:
        raise Fail("Cloud Run service describe JSON missing latest ready revision")
    add_protected_revision(protected, latest_ready[0], f"{latest_ready[1]} latest ready")

    latest_created = first_revision_ref(sources, CLOUD_RUN_LATEST_CREATED_FIELDS)
    if latest_created is None:
        raise Fail("Cloud Run service describe JSON missing latest created revision")
    add_protected_revision(protected, latest_created[0], f"{latest_created[1]} latest created")

    for label, targets in (
        ("service.traffic", service.get("traffic")),
        ("service.trafficStatuses", service.get("trafficStatuses")),
        ("status.traffic", status_obj.get("traffic")),
        ("status.trafficStatuses", status_obj.get("trafficStatuses")),
        ("spec.traffic", spec_obj.get("traffic")),
    ):
        protect_cloud_run_traffic_revisions(protected, label, targets)
    return {name: tuple(sorted(reasons)) for name, reasons in sorted(protected.items())}


def select_cloud_run_revision_cleanup(
    service: Any,
    raw_revisions: Any,
    keep: int,
) -> CloudRunRevisionCleanupPlan:
    if keep < 1:
        raise Fail("--keep must be at least 1")
    revisions = parse_cloud_run_revisions(raw_revisions)
    protected: dict[str, set[str]] = {
        name: set(reasons)
        for name, reasons in cloud_run_service_protected_revisions(service).items()
    }
    for revision in revisions[:keep]:
        add_protected_revision(protected, revision.name, f"latest {keep}")
    delete_revisions = tuple(
        revision
        for revision in revisions[keep:]
        if revision.name not in protected
    )
    return CloudRunRevisionCleanupPlan(
        total_revisions=len(revisions),
        keep_latest=keep,
        protected_revisions={
            name: tuple(sorted(reasons))
            for name, reasons in sorted(protected.items())
        },
        delete_revisions=delete_revisions,
    )


def print_cloud_run_revision_cleanup_plan(
    project: str,
    region: str,
    service: str,
    plan: CloudRunRevisionCleanupPlan,
) -> None:
    print(
        "Cloud Run revision cleanup plan "
        f"project={project} region={region} service={service}"
    )
    print(f"total_revisions={plan.total_revisions}")
    print(f"keep_latest={plan.keep_latest}")
    print(f"retained_after_cleanup={plan.retained_after_cleanup}")
    print(f"protected_revisions={len(plan.protected_revisions)}")
    if plan.protected_revisions:
        for name, reasons in plan.protected_revisions.items():
            print(f"  - {name}: {', '.join(reasons)}")
    else:
        print("  - none")
    print(f"delete_revisions={len(plan.delete_revisions)}")
    if plan.delete_revisions:
        for revision in plan.delete_revisions:
            print(f"  - {revision.name} created_at={revision.created_at_raw}")
    else:
        print("  - none")


def cmd_cleanup_cloud_run_revisions(args: argparse.Namespace) -> None:
    project = require_non_empty_arg(args.project, "--project")
    region = require_non_empty_arg(args.region, "--region")
    service = require_non_empty_arg(args.service, "--service")
    if args.keep < 1:
        raise Fail("--keep must be at least 1")
    service_json = read_gcloud_json(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service,
            "--project",
            project,
            "--region",
            region,
            "--format=json",
        ],
        "Cloud Run service describe",
    )
    revisions_json = read_gcloud_json(
        [
            "gcloud",
            "run",
            "revisions",
            "list",
            "--service",
            service,
            "--project",
            project,
            "--region",
            region,
            "--format=json",
        ],
        "Cloud Run revisions list",
    )
    plan = select_cloud_run_revision_cleanup(service_json, revisions_json, args.keep)
    print_cloud_run_revision_cleanup_plan(project, region, service, plan)
    for revision in plan.delete_revisions:
        run(
            [
                "gcloud",
                "run",
                "revisions",
                "delete",
                revision.name,
                "--project",
                project,
                "--region",
                region,
                "--quiet",
            ]
        )
    print(f"deleted_revisions={len(plan.delete_revisions)}")


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


def required_index(text: str, marker: str, context: str) -> int:
    index = text.find(marker)
    if index < 0:
        raise Fail(f"{context} is missing {marker!r}")
    return index


def assert_publish_workflow_order(
    name: str, workflow: str, branch_push: str, pre_resolve_marker: str
) -> None:
    push_image = required_index(workflow, "- name: Push image", name)
    render_tree = required_index(workflow, "publish.py render-tree", name)
    pre_resolve = required_index(workflow, pre_resolve_marker, name)
    resolve_ref = required_index(workflow, "- name: Resolve runtime artifact ref", name)
    push_branch = required_index(workflow, branch_push, name)
    verify_branch = required_index(
        workflow,
        'git ls-remote origin "refs/heads/$TARGET_BRANCH"',
        name,
    )
    verify_remote_cache = required_index(workflow, "- name: Verify remote Nix cache", name)
    verify_remote_cache_command = required_index(workflow, "publish.py verify-remote-cache", name)
    deploy_step = required_index(workflow, "- name: Deploy Cloud Run", name)
    gcloud_deploy = required_index(workflow, "gcloud run deploy", name)
    revision_cleanup_step = required_index(workflow, "- name: Cleanup Cloud Run revisions", name)
    revision_cleanup_command = required_index(workflow, "publish.py cleanup-cloud-run-revisions", name)
    delete_run_record = required_index(workflow, "- name: Delete successful run record", name)
    pinned_env = "REMOTE_DEV_HOST_ARTIFACTS_REMOTE_DEV_BIN_REF=${{ steps.artifact.outputs.ref }}"
    if not (
        push_image
        < render_tree
        < pre_resolve
        < resolve_ref
        < push_branch
        < verify_branch
        < verify_remote_cache
        < verify_remote_cache_command
        < deploy_step
        < gcloud_deploy
        < revision_cleanup_step
        < revision_cleanup_command
        < delete_run_record
    ):
        raise Fail(
            f"{name} must push the image, render the target branch, resolve the final "
            "artifact SHA, push and verify the branch/cache, deploy Cloud Run, then clean up revisions"
        )
    if "--keep 20" not in workflow:
        raise Fail(f"{name} must keep the latest 20 Cloud Run revisions")
    for line in workflow.splitlines():
        if "cleanup-cloud-run-revisions" in line and "|| true" in line:
            raise Fail(f"{name} must fail when Cloud Run revision cleanup fails")
    if pinned_env not in workflow:
        raise Fail(f"{name} must pin Cloud Run to the resolved remote-dev-bin artifact ref")
    if "steps.deploy.outputs" in workflow:
        raise Fail(f"{name} must render from the image digest step, not a deploy step")
    if "- name: Push image and deploy Cloud Run" in workflow:
        raise Fail(f"{name} must not deploy Cloud Run before the target branch push")


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
    for line in combined.splitlines():
        if "gh run delete" in line and "--yes" in line:
            raise Fail("gh run delete does not accept --yes in the GitHub runner CLI")
    assert_publish_workflow_order(
        "publish-test",
        test,
        'git push --force origin "$TARGET_BRANCH"',
        "- name: Apply host-service-test retention",
    )
    assert_publish_workflow_order(
        "publish-release",
        release,
        'git push origin "$TARGET_BRANCH"',
        "- name: Commit release branch",
    )


def fake_cloud_run_revision(index: int) -> dict[str, Any]:
    created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=index)
    return {
        "metadata": {
            "name": f"svc-{index:02d}",
            "creationTimestamp": created_at.isoformat().replace("+00:00", "Z"),
        }
    }


def fake_cloud_run_service(
    latest_ready: str,
    latest_created: str,
    traffic: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": {
            "latestReadyRevisionName": latest_ready,
            "latestCreatedRevisionName": latest_created,
            "traffic": traffic or [],
        }
    }


def assert_cloud_run_revision_cleanup_model() -> None:
    small_revisions = [fake_cloud_run_revision(index) for index in range(20)]
    small_plan = select_cloud_run_revision_cleanup(
        fake_cloud_run_service("svc-19", "svc-19"),
        small_revisions,
        20,
    )
    if small_plan.delete_revisions:
        raise Fail("Cloud Run revision cleanup must not delete when total is within the keep limit")

    many_revisions = [fake_cloud_run_revision(index) for index in range(25)]
    stale_plan = select_cloud_run_revision_cleanup(
        fake_cloud_run_service("svc-24", "svc-24"),
        many_revisions,
        20,
    )
    if [revision.name for revision in stale_plan.delete_revisions] != [
        "svc-04",
        "svc-03",
        "svc-02",
        "svc-01",
        "svc-00",
    ]:
        raise Fail("Cloud Run revision cleanup must only delete revisions older than the keep window")

    protected_plan = select_cloud_run_revision_cleanup(
        fake_cloud_run_service(
            "svc-00",
            "svc-01",
            [
                {"revisionName": "svc-02", "percent": 100},
                {"revisionName": "svc-03", "tag": "stable", "percent": 0},
            ],
        ),
        many_revisions,
        20,
    )
    if [revision.name for revision in protected_plan.delete_revisions] != ["svc-04"]:
        raise Fail("Cloud Run revision cleanup must protect old latest, traffic, and tagged revisions")
    if protected_plan.retained_after_cleanup != 24:
        raise Fail("Cloud Run revision cleanup must allow protected revisions to keep count above the limit")

    v2_plan = select_cloud_run_revision_cleanup(
        {
            "latestReadyRevision": "projects/p/locations/us-west1/services/svc/revisions/v2-00",
            "latestCreatedRevision": "projects/p/locations/us-west1/services/svc/revisions/v2-00",
        },
        [
            {
                "name": "projects/p/locations/us-west1/services/svc/revisions/v2-00",
                "createTime": "2026-01-01T00:00:00.123456789Z",
            }
        ],
        20,
    )
    if v2_plan.delete_revisions:
        raise Fail("Cloud Run revision cleanup must accept v2 full resource revision references")

    for bad_revisions, expected in [
        ([{"metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"}}], "missing revision name"),
        ([{"metadata": {"name": "svc-bad"}}], "missing creation timestamp"),
        (
            [{"metadata": {"name": "svc-bad", "creationTimestamp": "not-a-timestamp"}}],
            "invalid RFC3339 timestamp",
        ),
    ]:
        try:
            select_cloud_run_revision_cleanup(
                fake_cloud_run_service("svc-00", "svc-00"),
                bad_revisions,
                20,
            )
        except Fail as error:
            if expected not in str(error):
                raise
        else:
            raise Fail(f"Cloud Run revision cleanup must reject revisions with {expected}")

    try:
        select_cloud_run_revision_cleanup({"status": {}}, small_revisions, 20)
    except Fail as error:
        if "missing latest ready revision" not in str(error):
            raise
    else:
        raise Fail("Cloud Run revision cleanup must reject service JSON without latest revision status")

    try:
        decode_gcloud_json("not-json", "test gcloud")
    except Fail as error:
        if "did not return JSON" not in str(error):
            raise
    else:
        raise Fail("Cloud Run revision cleanup must reject non-JSON gcloud output")

    try:
        select_cloud_run_revision_cleanup(
            fake_cloud_run_service("svc-00", "svc-00"),
            small_revisions,
            0,
        )
    except Fail as error:
        if "--keep must be at least 1" not in str(error):
            raise
    else:
        raise Fail("Cloud Run revision cleanup must reject non-positive keep counts")


def cmd_self_test(_: argparse.Namespace) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    release = target_config("release")
    test_arm = target_config("host-service-test", "aarch64")
    if release.branch != "main" or release.cloud != "prod" or release.confirm_prod != "remote-dev-host-prod":
        raise Fail("release target is not bound to main/prod")
    if test_arm.remote_dev_systems != ("aarch64-linux",) or test_arm.host_arches != ("aarch64",):
        raise Fail("test aarch64 target did not narrow the matrix")
    if SYSTEMS["aarch64-linux"].cargo_target != "aarch64-unknown-linux-musl":
        raise Fail("linux aarch64 artifacts must use the static musl target")
    if SYSTEMS["x86_64-linux"].cargo_target != "x86_64-unknown-linux-musl":
        raise Fail("linux x86_64 artifacts must use the static musl target")
    assert_static_linux_elf_report(
        "static-test",
        "Elf file type is EXEC\nThere is no dynamic section in this file.",
    )
    for report in [
        "Program Headers:\n  INTERP         0x0000000000000238",
        "Dynamic section at offset 0x2e10 contains 1 entries:\n 0x0000000000000001 (NEEDED) Shared library: [libc.so.6]",
    ]:
        try:
            assert_static_linux_elf_report("dynamic-test", report)
        except Fail as error:
            if "must be static" not in str(error):
                raise
        else:
            raise Fail("Linux ELF audit must reject dynamic artifacts")
    if len(build_matrix(release)["include"]) != 6:
        raise Fail(
            "release matrix must include four remote-dev binaries and two runtime binaries"
        )
    def fake_ami(name: str, arch: str, image_id: str, creation_date: str) -> dict[str, str]:
        return {
            "Name": name,
            "Architecture": arch,
            "ImageId": image_id,
            "CreationDate": creation_date,
            "State": "available",
            "RootDeviceName": "/dev/xvda",
        }

    full_coverage_index = {
        "us-a-1": {
            "Images": [
                fake_ami("nixos/25.11.1.111111111111-aarch64-linux", "arm64", "ami-arm-old-a", "2026-01-01T00:00:00.000Z"),
                fake_ami("nixos/25.11.1.111111111111-x86_64-linux", "x86_64", "ami-x86-old-a", "2026-01-01T00:00:01.000Z"),
                fake_ami("nixos/25.11.2.222222222222-aarch64-linux", "arm64", "ami-arm-new-a", "2026-02-01T00:00:00.000Z"),
                fake_ami("nixos/25.11.2.222222222222-x86_64-linux", "x86_64", "ami-x86-new-a", "2026-02-01T00:00:01.000Z"),
            ]
        },
        "us-b-1": {
            "Images": [
                fake_ami("nixos/25.11.1.111111111111-aarch64-linux", "arm64", "ami-arm-old-b", "2026-01-01T00:00:00.000Z"),
                fake_ami("nixos/25.11.1.111111111111-x86_64-linux", "x86_64", "ami-x86-old-b", "2026-01-01T00:00:01.000Z"),
                fake_ami("nixos/25.11.2.222222222222-aarch64-linux", "arm64", "ami-arm-new-b", "2026-02-01T00:00:00.000Z"),
                fake_ami("nixos/25.11.2.222222222222-x86_64-linux", "x86_64", "ami-x86-new-b", "2026-02-01T00:00:01.000Z"),
            ]
        },
    }
    selected_pin = select_aws_ami_pin(full_coverage_index, ("x86_64", "aarch64"))
    if selected_pin.rev_prefix != "222222222222":
        raise Fail("AWS AMI pin selection did not choose the latest full-coverage prefix")
    partial_index = {
        "us-a-1": {
            "Images": [
                fake_ami("nixos/25.11.1.aaaaaaaaaaaa-aarch64-linux", "arm64", "ami-partial-a", "2026-01-01T00:00:00.000Z"),
            ]
        },
        "us-b-1": {
            "Images": [
                fake_ami("nixos/25.11.2.bbbbbbbbbbbb-aarch64-linux", "arm64", "ami-partial-b", "2026-02-01T00:00:00.000Z"),
            ]
        },
    }
    try:
        select_aws_ami_pin(partial_index, ("aarch64",))
    except Fail as error:
        if "full region coverage" not in str(error):
            raise
    else:
        raise Fail("AWS AMI pin selection must fail closed on partial region coverage")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        json_dump(
            tmp_root / "templates/flake.lock",
            {"nodes": {"nixpkgs": {"locked": {"rev": "0" * 40}}}},
        )
        json_dump(
            tmp_root / AWS_AMI_PIN_METADATA,
            {
                "full_rev": "1" * 40,
                "rev_prefix": "1" * AWS_AMI_REV_PREFIX_LEN,
                "preferred_release_prefix": AWS_AMI_PREFERRED_RELEASE_PREFIX,
                "ami_names": {
                    "aarch64": f"nixos/25.11.1.{'1' * AWS_AMI_REV_PREFIX_LEN}-aarch64-linux"
                },
            },
        )
        try:
            assert_aws_ami_pin_metadata(tmp_root)
        except Fail as error:
            if "does not match AWS AMI pin metadata" not in str(error):
                raise
        else:
            raise Fail("AWS AMI pin metadata check must reject flake.lock mismatches")
    cache_examples = {
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-remote-dev-runtime-test": True,
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
    with tempfile.TemporaryDirectory() as tmp:
        cache_dir = Path(tmp) / NIX_CACHE_DIR
        cache_dir.mkdir()
        unsigned_path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-remote-dev-test"
        try:
            audit_closure_manifest(
                cache_dir,
                {"paths": [{"path": unsigned_path, "signatures": [], "narSize": 1024}]},
            )
        except Fail as error:
            if "missing remote-dev-bin narinfo" not in str(error):
                raise
        else:
            raise Fail("closure audit must reject unsigned paths without narinfo")
        narinfo_path(cache_dir, unsigned_path).write_text("StorePath: test\n")
        audit = audit_closure_manifest(
            cache_dir,
            {"paths": [{"path": unsigned_path, "signatures": [], "narSize": 1024}]},
        )
        if audit["closure_paths"] != 1 or audit["unsigned_paths"] != 1:
            raise Fail("closure audit did not count unsigned paths")
        assert_agent_runtime_closure_names(
            [
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-remote-dev-agent-runtime-aarch64-linux",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-remote-dev-aarch64-linux",
                "cccccccccccccccccccccccccccccccc-remote-dev-runtime-aarch64-linux",
            ]
        )
        for bad_name in [
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-gcc-15.1.0",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc-2.42-61",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-remote-dev-host-group-git-core-aarch64-linux",
        ]:
            try:
                assert_agent_runtime_closure_names([bad_name])
            except Fail as error:
                if "agent runtime closure" not in str(error):
                    raise
            else:
                raise Fail(f"agent runtime closure audit must reject {bad_name}")
        marker_audit = audit_closure_manifest(
            cache_dir,
            {
                "paths": [
                    {
                        "path": "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-amazon-ssm-agent-3.2.0",
                        "signatures": ["cache.nixos.org-1:test"],
                        "narSize": 2048,
                    },
                    {
                        "path": "/nix/store/cccccccccccccccccccccccccccccccc-git-doc-2.51.0",
                        "signatures": ["cache.nixos.org-1:test"],
                        "narSize": 8192,
                    },
                    {
                        "path": "/nix/store/dddddddddddddddddddddddddddddddd-nixos-manual-html",
                        "signatures": ["cache.nixos.org-1:test"],
                        "narSize": 4096,
                    },
                    {
                        "path": "/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-nix-manual",
                        "signatures": ["cache.nixos.org-1:test"],
                        "narSize": 1024,
                    },
                ]
            },
        )
        for marker in CLOSURE_AUDIT_MARKERS:
            if not marker_audit["marker_presence"][marker]:
                raise Fail(f"closure audit did not report marker {marker}")
        if marker_audit["top_nar_size_paths"][0]["path"] != (
            "/nix/store/cccccccccccccccccccccccccccccccc-git-doc-2.51.0"
        ):
            raise Fail("closure audit did not sort top Nar sizes")
        try:
            audit_closure_manifest(
                cache_dir,
                {
                    "paths": [
                        {
                            "path": "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-nixpkgs-source",
                            "signatures": ["cache.nixos.org-1:test"],
                            "narSize": BOOTSTRAP_SOURCE_MAX_BYTES + 1,
                        }
                    ]
                },
            )
        except Fail as error:
            if "source paths over" not in str(error):
                raise
        else:
            raise Fail("closure audit must reject large source paths")
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
    assert_cloud_run_revision_cleanup_model()
    assert_aws_ami_pin_metadata(repo_root)
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
        flake = (branch / "flake.nix").read_text()
        def host_group_block(group_id: str) -> str:
            marker = f"          {group_id} = mkHostGroupBundle {{"
            start = flake.find(marker)
            if start < 0:
                raise Fail(f"rendered flake is missing host group block {group_id}")
            ends = [
                flake.find(f"\n          {candidate['id']} = mkHostGroupBundle {{", start + 1)
                for candidate in HOST_GROUPS
                if candidate["id"] != group_id
            ]
            ends = [end for end in ends if end >= 0]
            fallback = flake.find("\n        };\n\n      mkTakeoverRunner", start)
            if fallback >= 0:
                ends.append(fallback)
            if not ends:
                raise Fail(f"rendered flake host group block {group_id} has no end")
            return flake[start : min(ends)]

        def block_contains_package(block: str, package: str) -> bool:
            pattern = r"(?<![A-Za-z0-9_.-])" + re.escape(package) + r"(?![A-Za-z0-9_.-])"
            return re.search(pattern, block) is not None

        group_inputs = {
            group["id"]: set(group["inputs"])
            for group in HOST_GROUPS
        }
        all_group_inputs = set().union(*group_inputs.values())
        for group_id, inputs in group_inputs.items():
            block = host_group_block(group_id)
            for package in sorted(inputs):
                if not block_contains_package(block, package):
                    raise Fail(f"rendered flake group {group_id} missing input {package}")
            for package in sorted(all_group_inputs - inputs):
                if block_contains_package(block, package):
                    raise Fail(f"rendered flake group {group_id} includes input owned by another group: {package}")
        default_prefill_block = host_group_block("default-dev-shell-prefill")
        for package in ["pkgs.coreutils", "pkgs.curl", "pkgs.gnumake", "pkgs.openssh", "pkgs.pkg-config"]:
            if block_contains_package(default_prefill_block, package):
                raise Fail(f"default-dev-shell-prefill must not retain overlapping input {package}")
        git_core_block = host_group_block("git-core")
        for command in GIT_CORE_COMMANDS:
            expected = f'(mkHostGroupCommand pkgs.gitMinimal "{command}")'
            if expected not in git_core_block:
                raise Fail(f"rendered flake git-core missing command {command}")
        for stale in ["gitFull", "pkgs.git ", "pkgs.git\n"]:
            if stale in git_core_block:
                raise Fail(f"rendered flake git-core must stay on gitMinimal, found {stale.strip()}")
        for expected in [
            'host_config_id = "remote-dev-agent-runtime-v2";',
            "remote-dev-runtime = mkLocalBinaryPackage",
            "remote-dev-agent-runtime = mkAgentRuntimePackage pkgs remote-dev remote-dev-runtime;",
            'mkAgentRuntimePackage = pkgs: remoteDevPackage: runtimePackage:',
            'hostGroupPackages = system: pkgs:',
            'host-base-tools = mkHostGroupBundle {',
            'name = "host-base-tools";',
            'mkHostGroupCommand = package: command:',
            'git-core = mkHostGroupBundle {',
            'name = "git-core";',
            'mosh-transport = mkHostGroupBundle {',
            'name = "mosh-transport";',
            'shell-startup = mkHostGroupBundle {',
            'name = "shell-startup";',
            'build-baseline = mkHostGroupBundle {',
            'name = "build-baseline";',
            'c-toolchain-gcc = mkHostGroupBundle {',
            'name = "c-toolchain-gcc";',
            'c-toolchain-clang = mkHostGroupBundle {',
            'name = "c-toolchain-clang";',
            'name = "dev-diagnostics";',
            '"remote-dev-host-group-git-core" = (hostGroupPackages system pkgs)."git-core";',
            "remoteDevPackage",
            "runtimePackage",
            "pkgs.bashInteractive",
            "pkgs.clang",
            "pkgs.file",
            "pkgs.fzf",
            "pkgs.gcc",
            "pkgs.glibc.bin",
            "pkgs.gnumake",
            "pkgs.mosh",
            "pkgs.patchelf",
            "pkgs.pkg-config",
            "pkgs.starship",
            "pkgs.strace",
            "pkgs.zsh",
            "pkgs.zstd",
            "pkgs.gitMinimal",
            "documentation.enable = false;",
            "documentation.nixos.enable = false;",
            "documentation.man.enable = false;",
            "documentation.man.man-db.enable = false;",
            "documentation.info.enable = false;",
            "documentation.doc.enable = false;",
            "services.amazon-ssm-agent.enable = lib.mkForce false;",
            "nixpkgs.flake.setFlakeRegistry = false;",
            "nixpkgs.flake.setNixPath = false;",
            "nix.registry = lib.mkForce { };",
            "nix.nixPath = lib.mkForce [ ];",
            "nix.channel.enable = false;",
        ]:
            if expected not in flake:
                raise Fail(f"rendered flake is missing {expected}")
        if "ignoreCollisions" in flake:
            raise Fail("host group bundles must not ignore path collisions")
        if "devShells" in flake or "mkShellNoCC" in flake or "mkHostShell" in flake:
            raise Fail("host groups must be packages, not devShells")
        agent_runtime_block = flake.split("mkAgentRuntimePackage =", 1)[1].split("hostGroupPackages", 1)[0]
        for group_tool in sorted(
            {
                package
                for group in HOST_GROUPS
                for package in group["inputs"]
                if package.startswith("pkgs.")
            }
        ):
            if group_tool in agent_runtime_block:
                raise Fail(f"agent runtime must not include host group tool {group_tool}")
        host_base_block = host_group_block("host-base-tools")
        for package in [
            "pkgs.bash",
            "pkgs.coreutils",
            "pkgs.curl",
            "pkgs.iproute2",
            "pkgs.nix",
            "pkgs.openssh",
            "pkgs.systemd",
            "pkgs.util-linux",
        ]:
            if not block_contains_package(host_base_block, package):
                raise Fail(f"host-base-tools missing input {package}")
        if any(line.strip() == "pkgs.git" for line in flake.splitlines()):
            raise Fail("host baseline must use pkgs.gitMinimal instead of pkgs.git")
        spec_path = branch / "host-runtime-specs/aarch64.json"
        spec = json_load(spec_path)
        if spec["schema_version"] != 9:
            raise Fail("runtime spec did not use schema v9")
        if spec["firstboot_schema_version"] != 2:
            raise Fail("runtime spec did not use firstboot schema v2")
        if spec["aws_official_nixos_ami"]["preferred_release_prefix"] != AWS_AMI_PREFERRED_RELEASE_PREFIX:
            raise Fail("runtime spec did not include the preferred AMI release prefix")
        if spec["baseline_id"] != "remote-dev-agent-runtime-v2":
            raise Fail("runtime spec did not use the agent runtime contract id")
        if spec["bootstrap"]["agent_runtime_attr"] != "packages.aarch64-linux.remote-dev-agent-runtime":
            raise Fail("runtime spec did not point at the agent runtime package")
        if spec["bootstrap"]["agent_runtime_closure_manifest_file"] != "cloud/agent-runtime-closure-aarch64-linux.json":
            raise Fail("runtime spec did not point at the agent runtime closure manifest")
        if not spec["bootstrap"]["agent_runtime_store_path"].endswith("-remote-dev-agent-runtime-aarch64-linux"):
            raise Fail("runtime spec did not include agent runtime store path")
        if spec["bootstrap"]["agent_runtime_fingerprint"] != agent_runtime_fingerprint(
            "aarch64-linux", spec["bootstrap"]["agent_runtime_store_path"]
        ):
            raise Fail("runtime spec agent runtime fingerprint mismatch")
        if not (branch / "cloud/agent-runtime-closure-aarch64-linux.json").is_file():
            raise Fail("runtime closure manifest was not rendered")
        if spec["bootstrap"]["host_groups_catalog_file"] != "cloud/host-groups-catalog-aarch64-linux.json":
            raise Fail("runtime spec did not point at the host groups catalog")
        catalog = json_load(branch / "cloud/host-groups-catalog-aarch64-linux.json")
        if catalog["schema_version"] != 2:
            raise Fail("host groups catalog did not use schema v2")
        if catalog["fingerprint"] != spec["bootstrap"]["host_groups_catalog_fingerprint"]:
            raise Fail("runtime spec host groups catalog fingerprint mismatch")
        groups = {group["id"]: group for group in catalog["groups"]}
        if set(groups) != {group["id"] for group in HOST_GROUPS}:
            raise Fail("host groups catalog group set mismatch")
        if groups["default-dev-shell-prefill"]["commands"] != []:
            raise Fail("default dev shell prefill must not expose fake commands")
        host_base_commands = [shim["command"] for shim in groups["host-base-tools"]["commands"]]
        if host_base_commands != list(HOST_BASE_COMMANDS):
            raise Fail("host-base-tools command surface mismatch")
        host_default_groups = sorted(
            group_id for group_id, group in groups.items() if "host-default" in group["labels"]
        )
        if host_default_groups != ["host-base-tools"]:
            raise Fail(f"host-default warmup must only select host-base-tools, got {host_default_groups}")
        git_commands = [shim["command"] for shim in groups["git-core"]["commands"]]
        if git_commands != list(GIT_CORE_COMMANDS):
            raise Fail("git-core must expose the full gitMinimal command surface")
        if spec["bootstrap"]["git_core_group_store_path"] != groups["git-core"]["store_path"]:
            raise Fail("runtime spec git-core store path mismatch")
        if spec["bootstrap"]["git_core_group_fingerprint"] != groups["git-core"]["fingerprint"]:
            raise Fail("runtime spec git-core fingerprint mismatch")
        shell_commands = [shim["command"] for shim in groups["shell-startup"]["commands"]]
        if shell_commands != ["zsh", "starship"]:
            raise Fail("shell-startup must only expose zsh and starship")
        build_commands = [shim["command"] for shim in groups["build-baseline"]["commands"]]
        if build_commands != ["pkg-config", "make"]:
            raise Fail("build-baseline must only expose pkg-config and make")
        gcc_commands = [shim["command"] for shim in groups["c-toolchain-gcc"]["commands"]]
        if gcc_commands != ["cc", "gcc"]:
            raise Fail("c-toolchain-gcc must only expose cc and gcc")
        clang_commands = [shim["command"] for shim in groups["c-toolchain-clang"]["commands"]]
        if clang_commands != ["clang"]:
            raise Fail("c-toolchain-clang must only expose clang")
        if groups["c-toolchain-clang"]["priority"] != 200:
            raise Fail("c-toolchain-clang must stay late in the background queue")
        diagnostic_commands = [shim["command"] for shim in groups["dev-diagnostics"]["commands"]]
        if diagnostic_commands != ["strace", "file", "ldd"]:
            raise Fail("dev-diagnostics must only expose diagnostic commands")
        for group in groups.values():
            if "installable" in group or "scope" in group or "policy" in group:
                raise Fail("host groups catalog retained shell schema fields")
            if not group.get("store_path"):
                raise Fail("host groups group missing store_path")
            for command in group["commands"]:
                if not command.get("relative_path"):
                    raise Fail("host groups command missing relative_path")
            if not (branch / group["closure_manifest_file"]).is_file():
                raise Fail("host groups closure manifest was not rendered")
        if (branch / AWS_BOOTSTRAP_FLAKE).exists() or (branch / AWS_BOOTSTRAP_LOCK).exists():
            raise Fail("AWS bootstrap flake must not be rendered for host runtime firstboot")
        if (branch / "host-image-specs/x86_64.json").exists():
            raise Fail("aarch64-only publish rendered x86_64 host spec")
        if (branch / "host-runtime-specs/x86_64.json").exists():
            raise Fail("aarch64-only publish rendered x86_64 runtime spec")
        if (branch / "cloud/host-groups-catalog-x86_64-linux.json").exists():
            raise Fail("aarch64-only publish rendered x86_64 host groups catalog")
        if required_remote_cache_entries(branch, test_arm):
            raise Fail("placeholder render must not require remote cache entries")
        concrete_path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-remote-dev-agent-runtime-aarch64-linux"
        concrete_nar = branch / NIX_CACHE_DIR / "nar/test.nar.xz"
        concrete_nar.parent.mkdir(parents=True, exist_ok=True)
        concrete_nar.write_bytes(b"nar")
        concrete_narinfo = narinfo_path(branch / NIX_CACHE_DIR, concrete_path)
        concrete_narinfo.write_text(f"StorePath: {concrete_path}\nURL: nar/test.nar.xz\n")
        spec = json_load(spec_path)
        spec["bootstrap"]["agent_runtime_store_path"] = concrete_path
        json_dump(spec_path, spec)
        closure_path = branch / "cloud/agent-runtime-closure-aarch64-linux.json"
        closure = json_load(closure_path)
        closure["agent_runtime_store_path"] = concrete_path
        closure["paths"] = [{"path": concrete_path, "signatures": [], "narSize": 3}]
        json_dump(closure_path, closure)
        entries = required_remote_cache_entries(branch, test_arm)
        if len(entries) != 1 or entries[0].store_path != concrete_path:
            raise Fail("remote cache entry collection did not include concrete agent runtime")
        if entries[0].narinfo_relative != f"{NIX_CACHE_DIR}/{concrete_narinfo.name}":
            raise Fail("remote cache entry collection produced the wrong narinfo path")
        concrete_nar.unlink()
        try:
            required_remote_cache_entries(branch, test_arm)
        except Fail as error:
            if "referenced Nar file is missing" not in str(error):
                raise
        else:
            raise Fail("remote cache entry collection must reject missing Nar files")
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

    audit = sub.add_parser("audit-binary")
    audit.add_argument("--cargo-target", required=True)
    audit.add_argument("--artifact", required=True)
    audit.add_argument("--binary", required=True)
    audit.set_defaults(func=cmd_audit_binary)

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

    remote_cache = sub.add_parser("verify-remote-cache")
    remote_cache.add_argument("--target", choices=["release", "host-service-test"], required=True)
    remote_cache.add_argument("--arch", choices=["x86_64", "aarch64", "both"], default="both")
    remote_cache.add_argument("--branch-dir", required=True)
    remote_cache.add_argument("--artifact-sha", required=True)
    remote_cache.add_argument("--attempts", type=int, default=6)
    remote_cache.add_argument("--sleep-secs", type=float, default=5.0)
    remote_cache.set_defaults(func=cmd_verify_remote_cache)

    pin = sub.add_parser("refresh-aws-ami-pin")
    pin.add_argument("--target", choices=["release", "host-service-test"], default="host-service-test")
    pin.add_argument("--arch", choices=["x86_64", "aarch64", "both"])
    pin.add_argument("--images-json")
    pin.set_defaults(func=cmd_refresh_aws_ami_pin)

    verify_pin = sub.add_parser("verify-aws-ami-pin")
    verify_pin.add_argument("--target", choices=["release", "host-service-test"], default="host-service-test")
    verify_pin.add_argument("--arch", choices=["x86_64", "aarch64", "both"])
    verify_pin.add_argument("--images-json")
    verify_pin.set_defaults(func=cmd_verify_aws_ami_pin)

    c = sub.add_parser("cleanup-test-branch")
    c.add_argument("--branch-dir", required=True)
    c.add_argument("--branch", required=True)
    c.add_argument("--max-commits", type=int, default=5)
    c.add_argument("--max-days", type=int, default=7)
    c.set_defaults(func=cmd_cleanup_test_branch)

    cr = sub.add_parser("cleanup-cloud-run-revisions")
    cr.add_argument("--project", required=True)
    cr.add_argument("--region", required=True)
    cr.add_argument("--service", required=True)
    cr.add_argument("--keep", type=int, default=20)
    cr.set_defaults(func=cmd_cleanup_cloud_run_revisions)

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
