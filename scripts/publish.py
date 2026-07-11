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
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


REPO = "M-Adoo/remote-dev-bin"
SOURCE_REPO = "M-Adoo/remote-dev"
HOST_BUNDLE_SCHEMA_VERSION = 1
FIRSTBOOT_SCHEMA_VERSION = 3
HOST_GROUPS_CATALOG_SCHEMA_VERSION = 3
HOST_DEFAULT_SHELL_CONTRACT_ID = "remote-dev-default-shell-v2"
HOST_DEFAULT_SHELL_GROUP_ID = "default-dev-shell-prefill"
HOST_RUNTIME_ROOT = "/var/lib/remote-dev/runtime"
NIX_CACHE_DIR = "nix-cache"
ARTIFACT_DIR = "artifacts"
HOST_GROUPS_DIR = "cloud/host-groups"
GITHUB_MAX_BLOB_BYTES = 100_000_000
BOOTSTRAP_SOURCE_MAX_BYTES = 5 * 1024 * 1024
CLOSURE_AUDIT_TOP_NAR_PATHS = 10
MAX_AGENT_RUNTIME_CLOSURE_PATHS = 3
CLOSURE_AUDIT_MARKERS = (
    "amazon-ssm-agent",
    "git-doc",
    "nixos-manual-html",
    "nix-manual",
)
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
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

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
        "priority": 25,
        "labels": ["preconnect", "shell-baseline-nonblocking", "host-base", "host-tools"],
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
        "priority": 0,
        "labels": ["source-bootstrap", "workspace-sync", "git"],
        "inputs": ["pkgs.gitMinimal"],
        "commands": list(GIT_CORE_COMMANDS),
    },
    {
        "id": "mosh-transport",
        "priority": 10,
        "labels": ["preconnect", "terminal", "mosh", "transport"],
        "inputs": ["pkgs.mosh"],
        "commands": ["mosh-server"],
    },
    {
        "id": "default-dev-shell-prefill",
        "priority": 5,
        "labels": [
            "preconnect",
            "default-shell",
            "store-prefill",
        ],
        "inputs": ["defaultShellEnvProfile"],
        "commands": [],
    },
    {
        "id": "nix-source-baseline",
        "priority": 30,
        "labels": ["preconnect", "nix-source", "store-prefill"],
        "inputs": ["nixSourceBaseline"],
        "commands": [],
    },
    {
        "id": "shell-startup",
        "priority": 6,
        "labels": ["preconnect", "shell-baseline", "shell", "interactive", "startup"],
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
        "labels": ["build", "tooling", "background"],
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


@dataclass(frozen=True)
class GitHubDeployment:
    deployment_id: int
    environment: str
    created_at: dt.datetime
    created_at_raw: str
    ref: str | None
    sha: str | None


@dataclass(frozen=True)
class GitHubDeploymentCleanupPlan:
    repo: str
    environment: str
    cutoff: dt.datetime
    total_deployments: int
    delete_deployments: tuple[GitHubDeployment, ...]


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def target_config(name: str, arch_selection: str = "both") -> TargetConfig:
    if name == "release":
        return TargetConfig(
            name="release",
            branch="host-service-release",
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
    for path in branch_dir.iterdir():
        if path.name == ".git":
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
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
        default = "remote-dev" if any(m["kind"] == "remote-dev" for m in by_system[system]) else "remote-dev-runtime"
        lines.append(f"  default = {default};")
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n          // ".join(blocks) if blocks else "{}"


def render_flake(repo_root: Path, branch_dir: Path, config: TargetConfig, manifests: list[dict[str, Any]], version: str) -> None:
    template = (repo_root / "templates/flake.nix.in").read_text()
    rendered = (
        template.replace("__VERSION__", nix_string(version))
        .replace("__PACKAGES__", package_attrs(manifests))
    )
    unresolved = [token for token in ("__VERSION__", "__PACKAGES__") if token in rendered]
    if unresolved:
        raise Fail(f"rendered flake contains unresolved placeholders: {unresolved}")
    (branch_dir / "flake.nix").write_text(rendered)
    lock_template = repo_root / "templates/flake.lock"
    if lock_template.is_file():
        shutil.copy2(lock_template, branch_dir / "flake.lock")


def is_full_commit_sha(value: str) -> bool:
    return len(value) == 40 and all(byte in "0123456789abcdefABCDEF" for byte in value)


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


def host_runtime_group_env_snapshot_path(group_id: str) -> str:
    return f"{HOST_RUNTIME_ROOT}/groups/{group_id}/env"


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
            "schema_version": 1,
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
    return "host-groups-group-v3:" + hashlib.sha256(raw.encode()).hexdigest()


def host_groups_catalog_contracts(system: str, groups: list[dict[str, Any]]) -> list[dict[str, str]]:
    group_by_id = {group["id"]: group for group in groups}
    default_shell = group_by_id.get(HOST_DEFAULT_SHELL_GROUP_ID)
    if default_shell is None:
        raise Fail(f"host groups catalog is missing {HOST_DEFAULT_SHELL_GROUP_ID}")
    return [
        {
            "id": HOST_DEFAULT_SHELL_CONTRACT_ID,
            "group_id": HOST_DEFAULT_SHELL_GROUP_ID,
            "system": system,
            "fingerprint": default_shell["fingerprint"],
            "store_path": default_shell["store_path"],
            "env_snapshot": host_runtime_group_env_snapshot_path(HOST_DEFAULT_SHELL_GROUP_ID),
        }
    ]


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


def host_groups_catalog_fingerprint(
    system: str, contracts: list[dict[str, str]], groups: list[dict[str, Any]]
) -> str:
    raw = json.dumps(
        {
            "schema_version": HOST_GROUPS_CATALOG_SCHEMA_VERSION,
            "system": system,
            "contracts": contracts,
            "groups": groups,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "host-groups-catalog-v3:" + hashlib.sha256(raw.encode()).hexdigest()


def write_host_groups_catalogs(
    branch_dir: Path,
    config: TargetConfig,
    group_roots: dict[tuple[str, str], str] | None = None,
) -> dict[str, dict[str, str]]:
    catalogs: dict[str, dict[str, str]] = {}
    for arch in config.host_arches:
        system = f"{arch}-linux"
        groups = host_groups_catalog_groups(system, group_roots)
        contracts = host_groups_catalog_contracts(system, groups)
        fingerprint = host_groups_catalog_fingerprint(system, contracts, groups)
        catalog_file = host_groups_catalog_file(system)
        json_dump(
            branch_dir / catalog_file,
            {
                "schema_version": HOST_GROUPS_CATALOG_SCHEMA_VERSION,
                "system": system,
                "fingerprint": fingerprint,
                "contracts": contracts,
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
        if any(
            path_name_has_denied_marker(name, marker)
            for marker in AGENT_RUNTIME_CLOSURE_DENIED_MARKERS
        ):
            denied.append(raw_name)
    if unexpected:
        raise Fail("agent runtime closure contains non-agent paths: " + ", ".join(unexpected))
    if denied:
        raise Fail("agent runtime closure contains forbidden toolchain paths: " + ", ".join(denied))


def path_name_has_denied_marker(name: str, marker: str) -> bool:
    return re.search(rf"(^|-){re.escape(marker)}($|-)", name) is not None


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


def safe_extract_regular_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
                raise Fail(f"{archive}: unsafe tar path {member.name!r}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise Fail(f"{archive}: unsupported tar entry type for {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = source.extractfile(member)
            if extracted is None:
                raise Fail(f"{archive}: failed to read {member.name!r}")
            with target.open("wb") as output:
                shutil.copyfileobj(extracted, output)
            target.chmod(member.mode & 0o777)


def write_regular_tar(source_dir: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)

    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        if not (info.isfile() or info.isdir()):
            raise Fail(f"bundle source contains unsupported entry type: {info.name}")
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mtime = 0
        return info

    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as output:
        for path in sorted(source_dir.rglob("*")):
            output.add(path, arcname=path.relative_to(source_dir).as_posix(), recursive=False, filter=normalize)
    if archive.stat().st_size >= GITHUB_MAX_BLOB_BYTES:
        raise Fail(
            f"{archive.name} is {archive.stat().st_size} bytes; every published blob must be < "
            f"{GITHUB_MAX_BLOB_BYTES} bytes"
        )


def cli_bundle_flake(system: str) -> str:
    return f'''{{
  description = "remote-dev immutable {system} CLI bundle";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.flake-utils.url = "github:numtide/flake-utils";
  inputs.disko.url = "github:nix-community/disko";
  inputs.disko.inputs.nixpkgs.follows = "nixpkgs";
  outputs = {{ self, nixpkgs, flake-utils, disko }}:
    let
      system = "{system}";
      pkgs = nixpkgs.legacyPackages.${{system}};
      package = pkgs.stdenvNoCC.mkDerivation {{
        pname = "remote-dev";
        version = "bundle";
        src = ./.;
        dontUnpack = true;
        installPhase = ''
          install -Dm755 $src/bin/remote-dev $out/bin/remote-dev
        '';
      }};
    in {{
      packages.${{system}} = {{
        default = package;
        remote-dev = package;
      }};
      apps.${{system}}.default = {{ type = "app"; program = "${{package}}/bin/remote-dev"; }};
    }};
}}
'''


def package_cli_bundles(
    repo_root: Path, branch_dir: Path, manifests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    published: list[dict[str, Any]] = []
    for manifest in manifests:
        if manifest["kind"] != "remote-dev":
            continue
        artifact = manifest["artifact"]
        archive = branch_dir / ARTIFACT_DIR / f"{artifact}.tar.gz"
        with tempfile.TemporaryDirectory(prefix=f"{artifact}-") as raw_temp:
            temp = Path(raw_temp)
            extracted = temp / "extracted"
            bundle = temp / "bundle"
            safe_extract_regular_tar(archive, extracted)
            binaries = [path for path in extracted.rglob("remote-dev") if path.is_file()]
            if len(binaries) != 1:
                raise Fail(f"{archive.name}: expected exactly one remote-dev binary, got {len(binaries)}")
            (bundle / "bin").mkdir(parents=True)
            shutil.copy2(binaries[0], bundle / "bin/remote-dev")
            (bundle / "bin/remote-dev").chmod(0o755)
            (bundle / "flake.nix").write_text(cli_bundle_flake(manifest["system"]))
            shutil.copy2(repo_root / "templates/flake.lock", bundle / "flake.lock")
            write_regular_tar(bundle, archive)
        digest = sha256_file(archive)
        sha_path = Path(f"{archive}.sha256")
        sha_path.write_text(f"{digest}  {archive.name}\n")
        public = dict(manifest)
        public.update(
            {
                "schema_version": 2,
                "kind": "cli-bundle",
                "tarball": f"{ARTIFACT_DIR}/{archive.name}",
                "sha256": digest,
                "bundle_files": ["flake.nix", "flake.lock", "bin/remote-dev"],
            }
        )
        json_dump(branch_dir / ARTIFACT_DIR / f"{artifact}.build.json", public)
        published.append(public)
    return published


def copy_bundle_cache(branch_dir: Path, bundle_dir: Path, closure_files: list[str]) -> None:
    source_cache = branch_dir / NIX_CACHE_DIR
    target_cache = bundle_dir / NIX_CACHE_DIR
    target_cache.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_cache / "nix-cache-info", target_cache / "nix-cache-info")
    copied_nars: set[str] = set()
    for closure_file in closure_files:
        closure = json_load(branch_dir / closure_file)
        for store_path, _ in iter_nix_path_info(closure.get("paths")):
            source_narinfo = narinfo_path(source_cache, store_path)
            if not source_narinfo.is_file():
                continue
            target_narinfo = target_cache / source_narinfo.name
            shutil.copy2(source_narinfo, target_narinfo)
            fields = parse_narinfo_fields(source_narinfo.read_text(), str(source_narinfo))
            nar_relative = require_narinfo_relative_url(fields, str(source_narinfo))
            if nar_relative in copied_nars:
                continue
            source_nar = source_cache / nar_relative
            if not source_nar.is_file():
                raise Fail(f"{source_narinfo}: referenced NAR is missing: {nar_relative}")
            target_nar = target_cache / nar_relative
            target_nar.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_nar, target_nar)
            copied_nars.add(nar_relative)


def package_host_bundles(
    repo_root: Path,
    artifacts_dir: Path,
    branch_dir: Path,
    config: TargetConfig,
    source_ref: str,
    source_sha: str,
    trusted_key: str,
    agent_runtimes: dict[str, str],
    host_groups_catalogs: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    resources = {
        "bootstrap/run": artifacts_dir / "provider-bootstrap-run",
        "bootstrap/host-inspect.sh": artifacts_dir / "host-inspect.sh",
        "bootstrap/leasectl.sh": artifacts_dir / "leasectl.sh",
    }
    missing = [str(path) for path in resources.values() if not path.is_file()]
    if missing:
        raise Fail(f"host bundle resources are missing: {', '.join(missing)}")
    published: list[dict[str, Any]] = []
    for arch in config.host_arches:
        system = f"{arch}-linux"
        agent_runtime_store_path = agent_runtimes.get(
            system, placeholder_agent_runtime_store_path(system)
        )
        catalog_meta = host_groups_catalogs[system]
        catalog = json_load(branch_dir / catalog_meta["file"])
        git_core = next(
            (group for group in catalog["groups"] if group.get("id") == "git-core"), None
        )
        if not isinstance(git_core, dict):
            raise Fail(f"{catalog_meta['file']}: missing git-core group")
        closure_files = [agent_runtime_closure_manifest_file(system)] + [
            group["closure_manifest_file"] for group in catalog["groups"]
        ]
        artifact = f"remote-dev-host-{system}"
        archive = branch_dir / ARTIFACT_DIR / f"{artifact}.tar.gz"
        with tempfile.TemporaryDirectory(prefix=f"{artifact}-") as raw_temp:
            bundle = Path(raw_temp) / "bundle"
            bundle.mkdir(parents=True)
            for relative, source in resources.items():
                target = bundle / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                target.chmod(0o755 if relative == "bootstrap/run" else 0o700)
            for relative in closure_files + [catalog_meta["file"]]:
                target = bundle / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(branch_dir / relative, target)
            copy_bundle_cache(branch_dir, bundle, closure_files)
            bundle_manifest = {
                "bundle_schema_version": HOST_BUNDLE_SCHEMA_VERSION,
                "firstboot_schema_version": FIRSTBOOT_SCHEMA_VERSION,
                "source_repo": SOURCE_REPO,
                "source_ref": source_ref,
                "source_sha": source_sha,
                "system": system,
                "arch": arch,
                "nix_trusted_public_key": trusted_key,
                "agent_runtime_store_path": agent_runtime_store_path,
                "agent_runtime_fingerprint": agent_runtime_fingerprint(
                    system, agent_runtime_store_path
                ),
                "agent_runtime_closure_manifest_file": agent_runtime_closure_manifest_file(system),
                "host_groups_catalog_file": catalog_meta["file"],
                "host_groups_catalog_fingerprint": catalog_meta["fingerprint"],
                "git_core_group_store_path": git_core["store_path"],
                "git_core_group_fingerprint": git_core["fingerprint"],
                "flake_lock_hash": f"sha256:{sha256_file(repo_root / 'templates/flake.lock')}",
            }
            json_dump(bundle / "manifest.json", bundle_manifest)
            write_regular_tar(bundle, archive)
        digest = sha256_file(archive)
        Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n")
        public = {
            "schema_version": 1,
            "kind": "host-bundle",
            "target": config.name,
            "source_repo": SOURCE_REPO,
            "source_ref": source_ref,
            "source_sha": source_sha,
            "system": system,
            "arch": arch,
            "artifact": artifact,
            "tarball": f"{ARTIFACT_DIR}/{archive.name}",
            "sha256": digest,
            "bundle_schema_version": HOST_BUNDLE_SCHEMA_VERSION,
            "firstboot_schema_version": FIRSTBOOT_SCHEMA_VERSION,
        }
        json_dump(branch_dir / ARTIFACT_DIR / f"{artifact}.build.json", public)
        published.append(public)
    return published


def remove_bundle_staging_tree(branch_dir: Path, copied: list[dict[str, Any]]) -> None:
    for manifest in copied:
        if manifest["kind"] == "runtime":
            artifact = manifest["artifact"]
            for suffix in (".tar.gz", ".tar.gz.sha256", ".build.json"):
                path = branch_dir / ARTIFACT_DIR / f"{artifact}{suffix}"
                if path.exists():
                    path.unlink()
    for relative in ("flake.nix", "flake.lock", NIX_CACHE_DIR):
        path = branch_dir / relative
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    cloud = branch_dir / "cloud"
    if cloud.is_dir():
        for path in list(cloud.iterdir()):
            if path.name == "host-service-image.json":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


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
    write_nix_cache_info(branch_dir, trusted_key)
    agent_runtimes, group_roots = maybe_realize_runtime_and_cache(
        branch_dir, config, args.nix_cache_signing_key_file, trusted_key
    )
    host_groups_catalogs = write_host_groups_catalogs(branch_dir, config, group_roots)
    for arch in config.host_arches:
        system = f"{arch}-linux"
        closure_path = branch_dir / agent_runtime_closure_manifest_file(system)
        if not closure_path.is_file():
            store_path = agent_runtimes.get(system, placeholder_agent_runtime_store_path(system))
            json_dump(
                closure_path,
                {
                    "schema_version": 1,
                    "system": system,
                    "agent_runtime_attr": agent_runtime_attr(system),
                    "agent_runtime_store_path": store_path,
                    "paths": [store_path],
                },
            )
    closure_audits = (
        audit_runtime_closures(branch_dir, config)
        if args.nix_cache_signing_key_file
        else []
    )
    write_cloud_image_metadata(
        branch_dir, args, config, image_manifests[0] if image_manifests else None
    )
    cli_bundles = package_cli_bundles(repo_root, branch_dir, copied)
    host_bundles = package_host_bundles(
        repo_root,
        artifacts_dir,
        branch_dir,
        config,
        args.source_ref,
        args.source_sha,
        trusted_key,
        agent_runtimes,
        host_groups_catalogs,
    )
    published_artifacts = sorted(
        cli_bundles + host_bundles, key=lambda artifact: artifact["artifact"]
    )
    remove_bundle_staging_tree(branch_dir, copied)

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
        "artifacts": published_artifacts,
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
    expected_root = {"build-manifest.json", ARTIFACT_DIR, "cloud"}
    actual_root = {path.name for path in branch_dir.iterdir() if path.name != ".git"}
    if actual_root != expected_root:
        raise Fail(
            f"artifact commit root mismatch: expected={sorted(expected_root)}, "
            f"actual={sorted(actual_root)}"
        )
    cloud_files = {
        path.relative_to(branch_dir / "cloud").as_posix()
        for path in (branch_dir / "cloud").rglob("*")
        if path.is_file()
    }
    if cloud_files != {"host-service-image.json"}:
        raise Fail(f"cloud artifact surface mismatch: {sorted(cloud_files)}")
    manifest = json_load(branch_dir / "build-manifest.json")
    if manifest.get("schema_version") != 1:
        raise Fail("build-manifest schema_version mismatch")
    if manifest.get("target", {}).get("branch") != config.branch:
        raise Fail("build-manifest target branch mismatch")
    source_sha = manifest.get("source", {}).get("sha")
    if not isinstance(source_sha, str) or not is_full_commit_sha(source_sha):
        raise Fail("build-manifest source SHA must be a full commit SHA")

    expected_names = {
        *(f"remote-dev-{system}" for system in config.remote_dev_systems),
        *(f"remote-dev-host-{arch}-linux" for arch in config.host_arches),
    }
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise Fail("build-manifest artifacts must be a list")
    got_names = {artifact.get("artifact") for artifact in artifacts}
    if got_names != expected_names:
        raise Fail(
            f"published artifact set mismatch: expected={sorted(expected_names)}, "
            f"actual={sorted(str(name) for name in got_names)}"
        )
    expected_files: set[str] = set()
    for artifact in artifacts:
        name = artifact["artifact"]
        archive = branch_dir / ARTIFACT_DIR / f"{name}.tar.gz"
        checksum = Path(f"{archive}.sha256")
        build = branch_dir / ARTIFACT_DIR / f"{name}.build.json"
        expected_files.update({archive.name, checksum.name, build.name})
        for path in (archive, checksum, build):
            if not path.is_file():
                raise Fail(f"missing artifact file {path.relative_to(branch_dir)}")
            if path.stat().st_size >= GITHUB_MAX_BLOB_BYTES:
                raise Fail(f"published blob must be < {GITHUB_MAX_BLOB_BYTES} bytes: {path.name}")
        digest = sha256_file(archive)
        if digest != artifact.get("sha256"):
            raise Fail(f"{archive.name}: build-manifest checksum mismatch")
        checksum_fields = checksum.read_text().strip().split()
        if checksum_fields != [digest, archive.name]:
            raise Fail(f"{checksum.name}: invalid checksum sidecar")
        build_manifest = json_load(build)
        if build_manifest.get("sha256") != digest or build_manifest.get("source_sha") != source_sha:
            raise Fail(f"{build.name}: build metadata mismatch")
        with tempfile.TemporaryDirectory(prefix=f"validate-{name}-") as raw_temp:
            extracted = Path(raw_temp)
            safe_extract_regular_tar(archive, extracted)
            files = {
                path.relative_to(extracted).as_posix()
                for path in extracted.rglob("*")
                if path.is_file()
            }
            if artifact["kind"] == "cli-bundle":
                required = {"flake.nix", "flake.lock", "bin/remote-dev"}
                if files != required:
                    raise Fail(f"{archive.name}: CLI bundle files mismatch: {sorted(files)}")
                flake = (extracted / "flake.nix").read_text()
                if f'system = "{artifact["system"]}";' not in flake:
                    raise Fail(f"{archive.name}: CLI flake does not expose its system")
                for other in SYSTEMS:
                    if other != artifact["system"] and f'system = "{other}";' in flake:
                        raise Fail(f"{archive.name}: CLI flake exposes mismatched system {other}")
                if "releases/download" in flake or "fetchurl" in flake:
                    raise Fail(f"{archive.name}: CLI flake contains legacy download logic")
            elif artifact["kind"] == "host-bundle":
                for required in (
                    "manifest.json",
                    "bootstrap/run",
                    "bootstrap/host-inspect.sh",
                    "bootstrap/leasectl.sh",
                    "nix-cache/nix-cache-info",
                ):
                    if required not in files:
                        raise Fail(f"{archive.name}: missing host bundle file {required}")
                host_manifest = json_load(extracted / "manifest.json")
                if host_manifest.get("source_sha") != source_sha:
                    raise Fail(f"{archive.name}: source SHA mismatch")
                if host_manifest.get("system") != artifact["system"]:
                    raise Fail(f"{archive.name}: system mismatch")
                if host_manifest.get("bundle_schema_version") != HOST_BUNDLE_SCHEMA_VERSION:
                    raise Fail(f"{archive.name}: bundle schema mismatch")
                if host_manifest.get("firstboot_schema_version") != FIRSTBOOT_SCHEMA_VERSION:
                    raise Fail(f"{archive.name}: firstboot schema mismatch")
                flake_lock_hash = host_manifest.get("flake_lock_hash")
                if not isinstance(flake_lock_hash, str) or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", flake_lock_hash
                ):
                    raise Fail(f"{archive.name}: flake lock hash is missing or invalid")
                if any(
                    other in path
                    for other in SYSTEMS
                    if other != artifact["system"]
                    for path in files
                ):
                    raise Fail(f"{archive.name}: contains data for a different system")
            else:
                raise Fail(f"{name}: unsupported published artifact kind {artifact['kind']}")
    actual_files = {
        path.name for path in (branch_dir / ARTIFACT_DIR).iterdir() if path.is_file()
    }
    if actual_files != expected_files:
        raise Fail(
            f"artifacts directory mismatch: expected={sorted(expected_files)}, "
            f"actual={sorted(actual_files)}"
        )


def cmd_validate_tree(args: argparse.Namespace) -> None:
    validate_tree(Path(args.branch_dir), target_config(args.target, args.arch))


def cmd_verify_remote_cache(args: argparse.Namespace) -> None:
    branch_dir = Path(args.branch_dir)
    config = target_config(args.target, args.arch)
    validate_tree(branch_dir, config)
    manifest = json_load(branch_dir / "build-manifest.json")
    metadata_paths = ["build-manifest.json", "cloud/host-service-image.json"]
    for artifact in manifest["artifacts"]:
        name = artifact["artifact"]
        metadata_paths.extend(
            [
                f"artifacts/{name}.tar.gz.sha256",
                f"artifacts/{name}.build.json",
            ]
        )
        archive_relative = f"artifacts/{name}.tar.gz"
        archive_url = raw_github_artifact_url(args.artifact_sha, archive_relative)
        fetch_remote_bytes(
            archive_url,
            archive_relative,
            0,
            args.attempts,
            args.sleep_secs,
            method="HEAD",
        )
    for relative in metadata_paths:
        local = branch_dir / relative
        remote = fetch_remote_bytes(
            raw_github_artifact_url(args.artifact_sha, relative),
            relative,
            local.stat().st_size,
            args.attempts,
            args.sleep_secs,
        )
        if remote != local.read_bytes():
            raise Fail(f"{relative}: remote content does not match generated artifact commit")
    print(
        "remote bundle tree verified "
        f"target={config.name} "
        f"arch={','.join(config.host_arches)} "
        f"artifact_sha={args.artifact_sha} "
        f"artifacts={len(manifest['artifacts'])}"
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


def require_github_repo(value: str, name: str) -> str:
    repo = require_non_empty_arg(value, name)
    if GITHUB_REPO_RE.fullmatch(repo) is None:
        raise Fail(f"{name} must be in owner/repo form")
    return repo


def parse_rfc3339_timestamp(value: Any, context: str) -> dt.datetime:
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


def decode_gcloud_json(text: str, context: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise Fail(f"{context}: gcloud did not return JSON: {error}") from error


def read_gcloud_json(args: list[str], context: str) -> Any:
    return decode_gcloud_json(run(args, capture=True), context)


def decode_gh_json(text: str, context: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise Fail(f"{context}: GitHub API did not return JSON: {error}") from error


def read_gh_json(args: list[str], context: str) -> Any:
    return decode_gh_json(run(args, capture=True), context)


def cloud_run_revision_name_from_ref(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip().rstrip("/")
    if not trimmed:
        return None
    return trimmed.rsplit("/", 1)[-1]


def parse_cloud_run_rfc3339(value: Any, context: str) -> dt.datetime:
    return parse_rfc3339_timestamp(value, context)


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


def flatten_github_paginated_items(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise Fail(f"{context} JSON must be a list")
    if all(isinstance(page, list) for page in value):
        return [item for page in value for item in page]
    return value


def parse_github_deployments(value: Any) -> tuple[GitHubDeployment, ...]:
    deployments: list[GitHubDeployment] = []
    seen: set[int] = set()
    for index, raw in enumerate(flatten_github_paginated_items(value, "GitHub deployments API")):
        context = f"GitHub deployment[{index}]"
        if not isinstance(raw, dict):
            raise Fail(f"{context}: must be an object")
        deployment_id = raw.get("id")
        if not isinstance(deployment_id, int) or isinstance(deployment_id, bool):
            raise Fail(f"{context}: missing numeric id")
        if deployment_id in seen:
            raise Fail(f"duplicate GitHub deployment id {deployment_id}")
        seen.add(deployment_id)
        environment = raw.get("environment")
        if not isinstance(environment, str) or not environment:
            raise Fail(f"{context}: missing environment")
        created_at_raw = raw.get("created_at")
        created_at = parse_rfc3339_timestamp(created_at_raw, context)
        ref = raw.get("ref")
        sha = raw.get("sha")
        deployments.append(
            GitHubDeployment(
                deployment_id=deployment_id,
                environment=environment,
                created_at=created_at,
                created_at_raw=created_at_raw,
                ref=ref if isinstance(ref, str) and ref else None,
                sha=sha if isinstance(sha, str) and sha else None,
            )
        )
    return tuple(sorted(deployments, key=lambda item: (item.created_at, item.deployment_id)))


def select_github_test_deployment_cleanup(
    repo: str,
    environment: str,
    raw_deployments: Any,
    cutoff: dt.datetime,
) -> GitHubDeploymentCleanupPlan:
    if environment != "test":
        raise Fail("GitHub deployment cleanup is only allowed for environment=test")
    if cutoff.tzinfo is None:
        raise Fail("GitHub deployment cleanup cutoff must include a timezone")
    deployments = parse_github_deployments(raw_deployments)
    delete_deployments = tuple(
        deployment
        for deployment in deployments
        if deployment.environment == environment and deployment.created_at < cutoff.astimezone(dt.UTC)
    )
    return GitHubDeploymentCleanupPlan(
        repo=repo,
        environment=environment,
        cutoff=cutoff.astimezone(dt.UTC),
        total_deployments=len(deployments),
        delete_deployments=delete_deployments,
    )


def print_github_deployment_cleanup_plan(plan: GitHubDeploymentCleanupPlan) -> None:
    print(
        "GitHub deployment cleanup plan "
        f"repo={plan.repo} environment={plan.environment} "
        f"cutoff={plan.cutoff.isoformat().replace('+00:00', 'Z')}"
    )
    print(f"total_deployments={plan.total_deployments}")
    print(f"delete_deployments={len(plan.delete_deployments)}")
    if plan.delete_deployments:
        for deployment in plan.delete_deployments:
            suffix = ""
            if deployment.sha is not None:
                suffix += f" sha={deployment.sha}"
            if deployment.ref is not None:
                suffix += f" ref={deployment.ref}"
            print(
                f"  - id={deployment.deployment_id} "
                f"created_at={deployment.created_at_raw}{suffix}"
            )
    else:
        print("  - none")


def cmd_cleanup_github_test_deployments(args: argparse.Namespace) -> None:
    repo = require_github_repo(args.repo, "--repo")
    environment = require_non_empty_arg(args.environment, "--environment")
    if args.max_days <= 0:
        raise Fail("--max-days must be greater than 0")
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.max_days)
    deployments_json = read_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{repo}/deployments",
            "-f",
            f"environment={environment}",
            "-F",
            "per_page=100",
            "--paginate",
            "--slurp",
        ],
        "GitHub deployments list",
    )
    plan = select_github_test_deployment_cleanup(repo, environment, deployments_json, cutoff)
    print_github_deployment_cleanup_plan(plan)
    for deployment in plan.delete_deployments:
        run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{repo}/deployments/{deployment.deployment_id}/statuses",
                "-f",
                "state=inactive",
                "-f",
                "description=retention cleanup",
            ]
        )
        run(
            [
                "gh",
                "api",
                "--method",
                "DELETE",
                f"repos/{repo}/deployments/{deployment.deployment_id}",
            ]
        )
    print(f"deleted_deployments={len(plan.delete_deployments)}")


def fake_artifact(root: Path, manifest: dict[str, str]) -> None:
    name = manifest["artifact"]
    tarball = root / f"{name}.tar.gz"
    with tempfile.TemporaryDirectory(prefix=f"fake-{name}-") as raw_temp:
        bundle = Path(raw_temp)
        binary = bundle / manifest["binary"]
        binary.write_text(f"#!/bin/sh\necho {name}\n")
        binary.chmod(0o755)
        write_regular_tar(bundle, tarball)
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


def workflow_step(text: str, step_name: str, context: str) -> str:
    start = required_index(text, step_name, context)
    end = text.find("\n      - name:", start + 1)
    if end < 0:
        end = len(text)
    return text[start:end]


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
    provider_refresh_step = required_index(workflow, "- name: Refresh provider spots", name)
    provider_refresh_command = required_index(workflow, "/v1/host-admin/spots/refresh", name)
    provider_spots_verify = required_index(workflow, "provider-spots.json", name)
    revision_cleanup_step = required_index(workflow, "- name: Cleanup Cloud Run revisions", name)
    revision_cleanup_command = required_index(workflow, "publish.py cleanup-cloud-run-revisions", name)
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
        < provider_refresh_step
        < provider_refresh_command
        < provider_spots_verify
        < revision_cleanup_step
        < revision_cleanup_command
    ):
        raise Fail(
            f"{name} must push the image, render the target branch, resolve the final "
            "artifact SHA, push and verify the branch/cache, deploy Cloud Run, refresh and verify "
            "provider spots, then clean up revisions"
        )
    if "--keep 20" not in workflow:
        raise Fail(f"{name} must keep the latest 20 Cloud Run revisions")
    for line in workflow.splitlines():
        if "cleanup-cloud-run-revisions" in line and "|| true" in line:
            raise Fail(f"{name} must fail when Cloud Run revision cleanup fails")
    if pinned_env not in workflow:
        raise Fail(f"{name} must pin Cloud Run to the resolved remote-dev-bin artifact ref")
    if ".refresh.failed == 0" not in workflow:
        raise Fail(f"{name} must fail when provider spot refresh reports a failure")
    if "all(. == $sha)" not in workflow:
        raise Fail(f"{name} must verify every provider spot pins the published artifact SHA")
    if "--no-allow-unauthenticated" in workflow and "gcloud auth print-identity-token" not in workflow:
        raise Fail(f"{name} must authenticate protected provider spot refresh requests")
    if "steps.deploy.outputs" in workflow:
        raise Fail(f"{name} must render from the image digest step, not a deploy step")
    if "- name: Push image and deploy Cloud Run" in workflow:
        raise Fail(f"{name} must not deploy Cloud Run before the target branch push")


def assert_test_cleanup_workflow(cleanup: str) -> None:
    if "deployments: write" not in cleanup:
        raise Fail("cleanup workflow must have deployments: write permission")
    if "old_success_runs" in cleanup or "latest 5 successful" in cleanup or "-f status=success" in cleanup:
        raise Fail("cleanup workflow must not retain latest 5 successful run records")
    if '"repos/$GITHUB_REPOSITORY/actions/runs"' in cleanup:
        raise Fail("cleanup workflow must not enumerate all repository workflow runs")
    for workflow in (
        "publish-test.yml",
        "cleanup-host-service-test.yml",
        "delete-successful-test-run.yml",
    ):
        marker = f"repos/$GITHUB_REPOSITORY/actions/workflows/{workflow}/runs"
        if marker not in cleanup:
            raise Fail(f"cleanup workflow must clean old completed runs for {workflow}")
    deployment_step = workflow_step(cleanup, "- name: Delete old test deployments", "cleanup")
    if "cleanup-github-test-deployments" not in deployment_step:
        raise Fail("cleanup workflow must call cleanup-github-test-deployments")
    if "--environment test" not in deployment_step:
        raise Fail("cleanup workflow must only clean test deployments")
    if 'gh run delete "$GITHUB_RUN_ID"' in cleanup:
        raise Fail("cleanup workflow must not try to delete its active run")


def assert_successful_test_run_delete_workflow(workflow: str) -> None:
    if "workflow_run:" not in workflow:
        raise Fail("successful test run deleter must use workflow_run")
    for source in ("Publish Test Artifacts", "Cleanup HostService Test Artifacts"):
        if source not in workflow:
            raise Fail(f"successful test run deleter must watch {source}")
    if "github.event.workflow_run.conclusion == 'success'" not in workflow:
        raise Fail("successful test run deleter must only delete successful source runs")
    if "github.event.workflow_run.id" not in workflow:
        raise Fail("successful test run deleter must delete the completed source run id")
    if 'gh run delete "$SOURCE_RUN_ID"' not in workflow:
        raise Fail("successful test run deleter must delete SOURCE_RUN_ID")
    if 'gh run delete "$GITHUB_RUN_ID"' in workflow:
        raise Fail("successful test run deleter must not try to delete its active run")
    if "|| true" in workflow:
        raise Fail("successful test run deleter must fail loudly")
    if "actions: write" not in workflow:
        raise Fail("successful test run deleter must have actions: write permission")


def assert_workflows(repo_root: Path) -> None:
    test = (repo_root / ".github/workflows/publish-test.yml").read_text()
    release = (repo_root / ".github/workflows/publish-release.yml").read_text()
    cleanup = (repo_root / ".github/workflows/cleanup-host-service-test.yml").read_text()
    delete_successful_test_run = (
        repo_root / ".github/workflows/delete-successful-test-run.yml"
    ).read_text()
    combined = "\n".join([test, release, cleanup, delete_successful_test_run])
    if "pull_request" in combined or "pull_request_target" in combined:
        raise Fail("publish workflows must not run on pull_request events")
    for forbidden in ("cloud:", "project:"):
        if forbidden in test.split("jobs:", 1)[0]:
            raise Fail("publish-test must not expose cloud/project inputs")
        if forbidden in release.split("jobs:", 1)[0]:
            raise Fail("publish-release must not expose cloud/project inputs")
    if "environment: prod" not in release:
        raise Fail("publish-release must use the protected prod environment")
    if "TARGET_BRANCH: host-service-release" not in release:
        raise Fail("publish-release must target host-service-release")
    if "TARGET_BRANCH: host-service-test" not in test:
        raise Fail("publish-test must target host-service-test")
    if "REMOTE_DEV_CONFIRM_PROD" not in release or "remote-dev-host-prod" not in release:
        raise Fail("publish-release must carry the prod confirmation guard")
    if "contents: write" in test.split("publish:", 1)[0]:
        raise Fail("test build jobs must not receive contents write")
    for workflow_name, workflow in (("publish-test", test), ("publish-release", release)):
        publish_job = workflow.split("\n  publish:", 1)[1]
        if "REMOTE_DEV_READ_TOKEN" in publish_job:
            raise Fail(f"{workflow_name} publish job must not receive the private source token")
        for resource in ("provider-bootstrap-run", "host-inspect.sh", "leasectl.sh"):
            if resource not in workflow:
                raise Fail(f"{workflow_name} must export host bundle resource {resource}")
    if "git add -A" not in test or "git add -A" not in release:
        raise Fail("publish workflows must stage generated deletions with git add -A")
    for line in combined.splitlines():
        if "gh run delete" in line and "--yes" in line:
            raise Fail("gh run delete does not accept --yes in the GitHub runner CLI")
    if 'gh run delete "$GITHUB_RUN_ID"' in test:
        raise Fail("publish-test must not try to delete its active run")
    assert_test_cleanup_workflow(cleanup)
    assert_successful_test_run_delete_workflow(delete_successful_test_run)
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


def fake_github_deployment(
    deployment_id: int,
    environment: str,
    created_at: str,
    ref: str = "main",
    sha: str = "0123456789abcdef0123456789abcdef01234567",
) -> dict[str, Any]:
    return {
        "id": deployment_id,
        "environment": environment,
        "created_at": created_at,
        "ref": ref,
        "sha": sha,
    }


def assert_github_test_deployment_cleanup_model() -> None:
    cutoff = dt.datetime(2026, 1, 8, tzinfo=dt.UTC)
    raw = [
        fake_github_deployment(1, "test", "2026-01-01T00:00:00Z"),
        fake_github_deployment(2, "test", "2026-01-09T00:00:00Z"),
        fake_github_deployment(3, "prod", "2026-01-01T00:00:00Z"),
    ]
    plan = select_github_test_deployment_cleanup(
        "M-Adoo/remote-dev-bin",
        "test",
        raw,
        cutoff,
    )
    if [deployment.deployment_id for deployment in plan.delete_deployments] != [1]:
        raise Fail("GitHub deployment cleanup must only delete old test deployments")

    paginated_plan = select_github_test_deployment_cleanup(
        "M-Adoo/remote-dev-bin",
        "test",
        [raw[:2], raw[2:]],
        cutoff,
    )
    if [deployment.deployment_id for deployment in paginated_plan.delete_deployments] != [1]:
        raise Fail("GitHub deployment cleanup must accept paginated GitHub API output")

    try:
        select_github_test_deployment_cleanup(
            "M-Adoo/remote-dev-bin",
            "prod",
            raw,
            cutoff,
        )
    except Fail as error:
        if "environment=test" not in str(error):
            raise
    else:
        raise Fail("GitHub deployment cleanup must refuse non-test environments")

    for bad_deployments, expected in [
        ("not-a-list", "must be a list"),
        ([{"environment": "test", "created_at": "2026-01-01T00:00:00Z"}], "missing numeric id"),
        ([{"id": 1, "created_at": "2026-01-01T00:00:00Z"}], "missing environment"),
        ([{"id": 1, "environment": "test"}], "missing creation timestamp"),
    ]:
        try:
            select_github_test_deployment_cleanup(
                "M-Adoo/remote-dev-bin",
                "test",
                bad_deployments,
                cutoff,
            )
        except Fail as error:
            if expected not in str(error):
                raise
        else:
            raise Fail(f"GitHub deployment cleanup must reject deployments with {expected}")

    try:
        decode_gh_json("not-json", "test gh")
    except Fail as error:
        if "GitHub API did not return JSON" not in str(error):
            raise
    else:
        raise Fail("GitHub deployment cleanup must reject non-JSON GitHub API output")


def cmd_self_test(_: argparse.Namespace) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    release = target_config("release")
    test_arm = target_config("host-service-test", "aarch64")
    if (
        release.branch != "host-service-release"
        or release.cloud != "prod"
        or release.confirm_prod != "remote-dev-host-prod"
    ):
        raise Fail("release target is not bound to host-service-release/prod")
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
        assert_agent_runtime_closure_names(
            [
                "dddddddddddddddddddddddddddddddd-remote-dev-host-service-test.20260706T040315Z-gcc0b7cd233da-remote-dev-bin-test-cc0b7cd233da-1783310306",
                "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-remote-dev-runtime-host-service-test.20260706T040315Z-gcc0b7cd233da-remote-dev-bin-test-cc0b7cd233da-1783310306",
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
    assert_github_test_deployment_cleanup_model()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        artifacts = tmp_path / "artifacts"
        branch = tmp_path / "branch"
        artifacts.mkdir()
        for entry in build_matrix(test_arm)["include"]:
            fake_artifact(artifacts, {**entry, "target": "host-service-test"})
        for name in ("provider-bootstrap-run", "host-inspect.sh", "leasectl.sh"):
            resource = artifacts / name
            resource.write_text("#!/bin/sh\nset -eu\n")
            resource.chmod(0o755)
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
        validate_tree(branch, test_arm)
        root_entries = {path.name for path in branch.iterdir()}
        if root_entries != {"build-manifest.json", "artifacts", "cloud"}:
            raise Fail(f"single-commit artifact root mismatch: {sorted(root_entries)}")
        for stale in (
            "flake.nix",
            "flake.lock",
            "nix-cache",
            "host-runtime-specs",
            "host-image-specs",
            "cloud/agent-runtime-closure-aarch64-linux.json",
            "cloud/host-groups-catalog-aarch64-linux.json",
        ):
            if (branch / stale).exists():
                raise Fail(f"artifact tree retained staging path {stale}")
        build_manifest = json_load(branch / "build-manifest.json")
        if {artifact["kind"] for artifact in build_manifest["artifacts"]} != {
            "cli-bundle",
            "host-bundle",
        }:
            raise Fail("artifact tree did not publish only CLI and host bundles")
        cli_archive = branch / "artifacts/remote-dev-aarch64-linux.tar.gz"
        host_archive = branch / "artifacts/remote-dev-host-aarch64-linux.tar.gz"
        with tempfile.TemporaryDirectory(prefix="self-test-cli-") as raw_cli:
            cli = Path(raw_cli)
            safe_extract_regular_tar(cli_archive, cli)
            if {
                path.relative_to(cli).as_posix()
                for path in cli.rglob("*")
                if path.is_file()
            } != {"flake.nix", "flake.lock", "bin/remote-dev"}:
                raise Fail("CLI bundle shape mismatch")
            flake = (cli / "flake.nix").read_text()
            if 'system = "aarch64-linux";' not in flake:
                raise Fail("CLI bundle does not expose aarch64-linux")
            if 'system = "x86_64-linux";' in flake:
                raise Fail("CLI bundle exposes an unbuilt system")
        with tempfile.TemporaryDirectory(prefix="self-test-host-") as raw_host:
            host = Path(raw_host)
            safe_extract_regular_tar(host_archive, host)
            host_manifest = json_load(host / "manifest.json")
            if host_manifest["bundle_schema_version"] != HOST_BUNDLE_SCHEMA_VERSION:
                raise Fail("host bundle schema mismatch")
            if host_manifest["firstboot_schema_version"] != FIRSTBOOT_SCHEMA_VERSION:
                raise Fail("host bundle firstboot schema mismatch")
            if host_manifest["system"] != "aarch64-linux":
                raise Fail("host bundle system mismatch")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", host_manifest["flake_lock_hash"]):
                raise Fail("host bundle flake lock hash is invalid")
            if not (host / "bootstrap/run").is_file():
                raise Fail("host bundle entrypoint is missing")
            if any("x86_64-linux" in path.as_posix() for path in host.rglob("*")):
                raise Fail("aarch64 host bundle contains x86_64 data")
        unsafe = artifacts / "unsafe.tar.gz"
        with tarfile.open(unsafe, "w:gz") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 0
            archive.addfile(info)
        try:
            safe_extract_regular_tar(unsafe, tmp_path / "unsafe-extract")
        except Fail as error:
            if "unsafe tar path" not in str(error):
                raise
        else:
            raise Fail("safe tar extraction accepted a parent traversal")

        release_artifacts = tmp_path / "release-artifacts"
        release_branch = tmp_path / "release-branch"
        release_artifacts.mkdir()
        for entry in build_matrix(release)["include"]:
            fake_artifact(release_artifacts, {**entry, "target": "release"})
        for name in ("provider-bootstrap-run", "host-inspect.sh", "leasectl.sh"):
            resource = release_artifacts / name
            resource.write_text("#!/bin/sh\nset -eu\n")
            resource.chmod(0o755)
        cmd_render_tree(
            argparse.Namespace(
                target="release",
                arch="both",
                artifacts_dir=str(release_artifacts),
                branch_dir=str(release_branch),
                source_sha="0123456789abcdef0123456789abcdef01234567",
                source_ref="test-source",
                version="0.0.0-test",
                nix_cache_public_key="remote-dev-test:public",
                nix_cache_signing_key_file=None,
                image_digest="sha256:" + "2" * 64,
                image="us-west1-docker.pkg.dev/remote-dev-host-prod/remote-dev/host-service:test",
                deployed_image=None,
                workflow_run_id="2",
            )
        )
        validate_tree(release_branch, release)
        release_manifest = json_load(release_branch / "build-manifest.json")
        if release_manifest["target"]["branch"] != "host-service-release":
            raise Fail("release render did not target host-service-release")
        if len(release_manifest["artifacts"]) != 6:
            raise Fail("full release render did not produce four CLI and two host bundles")
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

    gd = sub.add_parser("cleanup-github-test-deployments")
    gd.add_argument("--repo", required=True)
    gd.add_argument("--environment", required=True)
    gd.add_argument("--max-days", type=int, default=7)
    gd.set_defaults(func=cmd_cleanup_github_test_deployments)

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
