#!/usr/bin/env python
"""Generic EIDE unify_builder wrapper.

This script powers the build-eide skill. It scans builder.params files,
resolves the underlying unify_builder executable, runs builds while streaming
the original output, and summarizes artifacts and size information.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
elif sys.stdout:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
elif sys.stderr:
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILLS_DIR = _SCRIPT_DIR.parent.parent
for _candidate in (_SKILLS_DIR / "shared", _SKILLS_DIR.parent / "shared"):
    if (_candidate / "tool_config.py").exists():
        sys.path.insert(0, str(_candidate))
        break
from tool_config import get_tool_path, set_tool_path


ARTIFACT_EXTENSIONS = {".axf": "elf", ".elf": "elf", ".hex": "hex", ".bin": "bin"}
ARTIFACT_PRIORITY = {"elf": 1, "hex": 2, "bin": 3}


@dataclass
class BuilderConfig:
    params_path: Path
    name: str
    target: str
    toolchain: str
    root_dir: Path
    out_dir: Path
    builder_dir: Path | None


@dataclass
class Artifact:
    path: Path
    kind: str
    size: int


@dataclass
class BuildResult:
    status: str
    summary: str
    build_cmd: str | None = None
    builder_params: str | None = None
    target_name: str | None = None
    toolchain: str | None = None
    out_dir: str | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    primary_artifact: Artifact | None = None
    program_size: dict[str, int] | None = None
    total_memory: dict[str, str] | None = None
    build_time: str | None = None
    errors: int = 0
    warnings: int = 0
    failure_category: str | None = None
    evidence: list[str] = field(default_factory=list)


def decode_output(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", sys.getfilesystemencoding() or "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def scan_builder_params(workspace: Path) -> list[Path]:
    results: list[Path] = []
    for root, _dirs, files in os.walk(workspace):
        depth = str(root).replace(str(workspace), "").count(os.sep)
        if depth > 4:
            continue
        for file_name in files:
            if file_name == "builder.params":
                results.append(Path(root) / file_name)
    results.sort()
    return results


def load_builder_config(params_path: Path) -> BuilderConfig:
    data = json.loads(params_path.read_text(encoding="utf-8"))
    env = data.get("env", {})

    root_dir = Path(data.get("rootDir") or env.get("ProjectRoot") or params_path.parent.parent)
    out_dir_value = data.get("outDir") or env.get("OutDir") or data.get("dumpPath") or "build"
    out_dir = Path(out_dir_value)
    if not out_dir.is_absolute():
        out_dir = root_dir / out_dir

    builder_dir_value = env.get("EIDE_BUILDER_DIR")
    builder_dir = Path(builder_dir_value) if builder_dir_value else None

    return BuilderConfig(
        params_path=params_path,
        name=data.get("name", params_path.parent.name),
        target=data.get("target", params_path.parent.name),
        toolchain=data.get("toolchain", "unknown"),
        root_dir=root_dir,
        out_dir=out_dir,
        builder_dir=builder_dir,
    )


def resolve_builder_exe(config: BuilderConfig, explicit: str | None = None, workspace: Path | None = None) -> str | None:
    candidates: list[Path | str] = []
    if explicit:
        candidates.append(explicit)

    configured = get_tool_path("unify_builder", workspace)
    if configured:
        candidates.append(configured)

    if config.builder_dir:
        candidates.append(config.builder_dir / "unify_builder.exe")
        candidates.append(config.builder_dir / "unify_builder")

    path_hit = shutil.which("unify_builder") or shutil.which("unify_builder.exe")
    if path_hit:
        candidates.append(path_hit)

    for candidate in candidates:
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return str(candidate_path)
    return None


def artifact_sort_key(artifact: Artifact) -> tuple[int, str]:
    return (ARTIFACT_PRIORITY.get(artifact.kind, 99), str(artifact.path).lower())


def scan_artifacts(config: BuilderConfig) -> list[Artifact]:
    artifacts: list[Artifact] = []
    if not config.out_dir.exists():
        return artifacts

    for root, _dirs, files in os.walk(config.out_dir):
        for file_name in files:
            kind = ARTIFACT_EXTENSIONS.get(Path(file_name).suffix.lower())
            if not kind:
                continue
            path = Path(root) / file_name
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            artifacts.append(Artifact(path=path, kind=kind, size=size))

    artifacts.sort(key=artifact_sort_key)
    return artifacts


def parse_build_metrics(output: str) -> tuple[dict[str, int] | None, dict[str, str] | None, str | None, int, int]:
    program_size = None
    total_memory = None
    build_time = None
    errors = 0
    warnings = 0

    match = re.search(r"Program Size:\s+Code=(\d+)\s+RO-data=(\d+)\s+RW-data=(\d+)\s+ZI-data=(\d+)", output)
    if match:
        program_size = {
            "code": int(match.group(1)),
            "ro_data": int(match.group(2)),
            "rw_data": int(match.group(3)),
            "zi_data": int(match.group(4)),
        }

    total_ro = re.search(r"Total RO\s+Size.*?\s+(\S+)/(\S+)", output)
    total_rw = re.search(r"Total RW\s+Size.*?\s+(\S+)/(\S+)", output)
    if total_ro or total_rw:
        total_memory = {}
        if total_ro:
            total_memory["rom_used"] = total_ro.group(1)
            total_memory["rom_total"] = total_ro.group(2)
        if total_rw:
            total_memory["ram_used"] = total_rw.group(1)
            total_memory["ram_total"] = total_rw.group(2)

    elapsed = re.search(r"elapsed time\s+([0-9:]+)", output, re.IGNORECASE)
    if elapsed:
        build_time = elapsed.group(1)

    errwarn = re.search(r"(\d+)\s+Error\(s\),\s+(\d+)\s+Warning\(s\)", output)
    if errwarn:
        errors = int(errwarn.group(1))
        warnings = int(errwarn.group(2))
    else:
        warnings = len(re.findall(r"\bwarning\b", output, re.IGNORECASE))
        errors = len(re.findall(r"\berror\b", output, re.IGNORECASE))

    return program_size, total_memory, build_time, errors, warnings


def print_artifacts(artifacts: list[Artifact]) -> None:
    if not artifacts:
        print("Artifacts: none")
        return

    print("Artifacts:")
    for artifact in artifacts:
        print(f"  - {artifact.path} ({artifact.kind}, {artifact.size} bytes)")
    primary = artifacts[0]
    print(f"STAR primary: {primary.path} ({primary.kind}, {primary.size} bytes)")
    print(f"STAR primary_raw: {primary.path}")


def resolve_builder_config(args) -> tuple[BuilderConfig | None, str | None]:
    if args.builder_params:
        params_path = Path(args.builder_params)
        if not params_path.is_file():
            return None, f"builder.params not found: {params_path}"
        return load_builder_config(params_path), None

    workspace = Path(args.workspace or os.getcwd())
    matches = scan_builder_params(workspace)
    if not matches:
        return None, f"No builder.params found under {workspace}"
    if len(matches) > 1:
        print("Multiple builder.params were found. Use --builder-params:")
        for index, item in enumerate(matches, 1):
            print(f"  {index}. {item}")
        return None, "Multiple builder.params candidates"
    return load_builder_config(matches[0]), None


def run_build(builder_exe: str, config: BuilderConfig) -> tuple[int, str, str]:
    cmd = [builder_exe, "-p", str(config.params_path)]
    print(f"$ {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        cwd=str(config.root_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    output_chunks: list[str] = []
    assert process.stdout is not None
    for raw_line in iter(process.stdout.readline, b""):
        if not raw_line:
            break
        line = decode_output(raw_line)
        output_chunks.append(line)
        print(line, end="")

    process.wait()
    return process.returncode, "".join(output_chunks), " ".join(cmd)


def print_build_summary(result: BuildResult) -> None:
    status_word = "SUCCESS" if result.status == "success" else "FAIL"
    print(f"Summary: {result.summary} [{status_word}]")
    if result.builder_params:
        print(f"  builder.params: {result.builder_params}")
    if result.target_name:
        print(f"  target: {result.target_name}")
    if result.toolchain:
        print(f"  toolchain: {result.toolchain}")
    if result.out_dir:
        print(f"  out_dir: {result.out_dir}")
    if result.program_size:
        print(
            "  Program Size: "
            f"Code={result.program_size['code']} "
            f"RO-data={result.program_size['ro_data']} "
            f"RW-data={result.program_size['rw_data']} "
            f"ZI-data={result.program_size['zi_data']}"
        )
    if result.total_memory:
        rom_info = ""
        ram_info = ""
        if "rom_used" in result.total_memory:
            rom_info = f"ROM {result.total_memory['rom_used']}/{result.total_memory['rom_total']}"
        if "ram_used" in result.total_memory:
            ram_info = f"RAM {result.total_memory['ram_used']}/{result.total_memory['ram_total']}"
        if rom_info or ram_info:
            print(f"  Memory: {rom_info} {ram_info}".strip())
    if result.build_time:
        print(f"  elapsed: {result.build_time}")
    print(f"  errors/warnings: {result.errors}/{result.warnings}")
    print_artifacts(result.artifacts)
    if result.failure_category:
        print(f"  failure_category: {result.failure_category}")
    for evidence in result.evidence[:5]:
        print(f"  evidence: {evidence}")


def cmd_detect(args) -> int:
    workspace = Path(args.workspace or os.getcwd())
    matches = scan_builder_params(workspace)
    print(f"Workspace: {workspace}")
    print(f"builder.params count: {len(matches)}")
    for index, item in enumerate(matches, 1):
        print(f"  {index}. {item}")

    config = None
    if args.builder_params:
        config, error = resolve_builder_config(args)
        if error:
            print(f"ERROR: {error}")
            return 1
    elif len(matches) == 1:
        config = load_builder_config(matches[0])

    if config:
        builder_exe = resolve_builder_exe(config, explicit=args.builder, workspace=workspace)
        print(f"target: {config.target}")
        print(f"toolchain: {config.toolchain}")
        print(f"out_dir: {config.out_dir}")
        print(f"unify_builder: {builder_exe or 'not found'}")
        if builder_exe and args.save_config:
            cfg_path = set_tool_path("unify_builder", builder_exe, workspace=workspace)
            print(f"saved unify_builder path to: {cfg_path}")
        return 0 if builder_exe else 1

    return 0 if matches else 1


def cmd_scan(args) -> int:
    workspace = Path(args.workspace or os.getcwd())
    matches = scan_builder_params(workspace)
    if not matches:
        print(f"ERROR: no builder.params found under {workspace}")
        return 1

    print("builder.params candidates:")
    for index, item in enumerate(matches, 1):
        config = load_builder_config(item)
        print(f"  {index}. {item} -> target={config.target}, toolchain={config.toolchain}, out_dir={config.out_dir}")
    return 0


def cmd_scan_artifacts(args) -> int:
    config, error = resolve_builder_config(args)
    if error or config is None:
        print(f"ERROR: {error}")
        return 1
    artifacts = scan_artifacts(config)
    print_artifacts(artifacts)
    return 0 if artifacts else 1


def cmd_build(args) -> int:
    config, error = resolve_builder_config(args)
    if error or config is None:
        result = BuildResult(
            status="blocked",
            summary="Build cannot start",
            failure_category="ambiguous-context" if "Multiple" in (error or "") else "environment-missing",
            evidence=[error] if error else [],
        )
        print_build_summary(result)
        return 1

    workspace = Path(args.workspace or config.root_dir)
    builder_exe = resolve_builder_exe(config, explicit=args.builder, workspace=workspace)
    if not builder_exe:
        result = BuildResult(
            status="blocked",
            summary="unify_builder was not found",
            builder_params=str(config.params_path),
            target_name=config.target,
            toolchain=config.toolchain,
            out_dir=str(config.out_dir),
            failure_category="environment-missing",
            evidence=["No valid unify_builder path found in builder.params, config, or PATH"],
        )
        print_build_summary(result)
        return 1

    return_code, output, build_cmd = run_build(builder_exe, config)
    artifacts = scan_artifacts(config)
    primary = artifacts[0] if artifacts else None
    program_size, total_memory, build_time, errors, warnings = parse_build_metrics(output)

    if return_code != 0:
        result = BuildResult(
            status="failure",
            summary="EIDE build failed",
            build_cmd=build_cmd,
            builder_params=str(config.params_path),
            target_name=config.target,
            toolchain=config.toolchain,
            out_dir=str(config.out_dir),
            artifacts=artifacts,
            primary_artifact=primary,
            program_size=program_size,
            total_memory=total_memory,
            build_time=build_time,
            errors=errors,
            warnings=warnings,
            failure_category="project-config-error",
            evidence=[line.strip() for line in output.splitlines() if "error" in line.lower()][:5],
        )
        print_build_summary(result)
        return 1

    if not artifacts:
        result = BuildResult(
            status="failure",
            summary="Build finished but no artifacts were found",
            build_cmd=build_cmd,
            builder_params=str(config.params_path),
            target_name=config.target,
            toolchain=config.toolchain,
            out_dir=str(config.out_dir),
            program_size=program_size,
            total_memory=total_memory,
            build_time=build_time,
            errors=errors,
            warnings=warnings,
            failure_category="artifact-missing",
        )
        print_build_summary(result)
        return 1

    result = BuildResult(
        status="success",
        summary="EIDE build succeeded",
        build_cmd=build_cmd,
        builder_params=str(config.params_path),
        target_name=config.target,
        toolchain=config.toolchain,
        out_dir=str(config.out_dir),
        artifacts=artifacts,
        primary_artifact=primary,
        program_size=program_size,
        total_memory=total_memory,
        build_time=build_time,
        errors=errors,
        warnings=warnings,
    )
    print_build_summary(result)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EIDE unify_builder helper")
    parser.add_argument("--workspace", help="Workspace directory")
    parser.add_argument("--builder-params", help="Explicit builder.params path")
    parser.add_argument("--builder", help="Explicit unify_builder executable path")
    parser.add_argument("--detect", action="store_true", help="Detect builder.params and unify_builder")
    parser.add_argument("--scan", action="store_true", help="Scan builder.params candidates")
    parser.add_argument("--build", action="store_true", help="Run build")
    parser.add_argument("--scan-artifacts", action="store_true", help="Scan artifacts only")
    parser.add_argument("--save-config", action="store_true", help="Save unify_builder path to config")
    parser.add_argument("-v", "--verbose", action="store_true", help="Compatibility flag")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.detect:
        return cmd_detect(args)
    if args.scan:
        return cmd_scan(args)
    if args.scan_artifacts:
        return cmd_scan_artifacts(args)
    if args.build:
        return cmd_build(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())