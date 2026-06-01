#!/usr/bin/env python
"""Generic EIDE flash wrapper.

This script resolves the active EIDE uploader configuration, picks the primary
artifact from the current builder output directory, and delegates flashing to
an existing low-level skill script while preserving terminal output.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
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


ARTIFACT_EXTENSIONS = {".axf": "elf", ".elf": "elf", ".hex": "hex", ".bin": "bin"}
ARTIFACT_PRIORITY = {"elf": 1, "hex": 2, "bin": 3}
JLINK_INTERFACE_MAP = {0: "JTAG", 1: "SWD"}

SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_DIR = SCRIPT_DIR.parent.parent
FLASH_JLINK_SCRIPT = SKILLS_DIR / "flash-jlink" / "scripts" / "jlink_flasher.py"


def print_failure(summary: str, category: str, evidence: str | None = None) -> int:
    print(f"Summary: {summary} [FAIL]")
    print(f"failure_category: {category}")
    if evidence:
        print(f"evidence: {evidence}")
    return 1


def print_success(summary: str) -> None:
    print(f"Summary: {summary} [SUCCESS]")


@dataclass
class BuilderConfig:
    params_path: Path
    target: str
    root_dir: Path
    out_dir: Path


@dataclass
class Artifact:
    path: Path
    kind: str
    size: int


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
    return BuilderConfig(
        params_path=params_path,
        target=data.get("target", params_path.parent.name),
        root_dir=root_dir,
        out_dir=out_dir,
    )


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


def parse_scalar(value: str):
    text = value.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        text = text[1:-1]
    if text.isdigit():
        return int(text)
    return text


def load_upload_config(root_dir: Path) -> tuple[str | None, dict[str, object], str | None]:
    config_path = root_dir / ".eide" / "eide.yml"
    if not config_path.is_file():
        return None, {}, f"EIDE upload config not found: {config_path}"

    active_uploader: str | None = None
    config_map: dict[str, object] = {}
    current_section: str | None = None
    current_nested: str | None = None
    in_upload_map = False
    upload_map_indent: int | None = None

    for raw_line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))

        if stripped == "uploadConfigMap:":
            in_upload_map = True
            upload_map_indent = indent
            current_section = None
            current_nested = None
            continue

        if stripped.startswith("uploader:"):
            active_uploader = stripped.split(":", 1)[1].strip().strip('"')
            continue

        if not in_upload_map:
            continue

        if upload_map_indent is None:
            continue

        if indent <= upload_map_indent and stripped.endswith(":") and stripped != "uploadConfigMap:":
            in_upload_map = False
            current_section = None
            current_nested = None
            continue

        if indent == upload_map_indent + 2 and stripped.endswith(":"):
            current_section = stripped[:-1]
            config_map.setdefault(current_section, {})
            current_nested = None
            continue

        if current_section is None or ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        if not value:
            current_nested = key
            section_obj = config_map.setdefault(current_section, {})
            if isinstance(section_obj, dict):
                section_obj.setdefault(key, {})
            continue

        section_obj = config_map.setdefault(current_section, {})
        if current_nested and indent > upload_map_indent + 4:
            nested_obj = section_obj.setdefault(current_nested, {})
            if isinstance(nested_obj, dict):
                nested_obj[key] = parse_scalar(value)
        elif isinstance(section_obj, dict):
            current_nested = None
            section_obj[key] = parse_scalar(value)

    if not active_uploader:
        return None, config_map, f"No uploader field found in {config_path}"
    return active_uploader, config_map, None


def build_jlink_command(args, artifact: Path, upload_config: dict[str, object]) -> list[str]:
    cpu_info = upload_config.get("cpuInfo", {}) if isinstance(upload_config, dict) else {}
    device = args.device or cpu_info.get("cpuName")
    if not device:
        raise RuntimeError("No J-Link device name was found in .eide/eide.yml or arguments")

    interface = args.interface
    if not interface:
        pro_type = upload_config.get("proType") if isinstance(upload_config, dict) else None
        interface = JLINK_INTERFACE_MAP.get(pro_type, "SWD")

    speed = args.speed
    if speed is None and isinstance(upload_config, dict):
        speed = upload_config.get("speed")
    if speed is None:
        speed = 4000

    cmd = [
        sys.executable,
        str(FLASH_JLINK_SCRIPT),
        "--artifact",
        str(artifact),
        "--device",
        str(device),
        "--interface",
        str(interface),
        "--speed",
        str(speed),
    ]

    base_address = args.base_address
    if not base_address and isinstance(upload_config, dict):
        base_address = upload_config.get("baseAddr")
    if base_address:
        cmd += ["--base-address", str(base_address)]

    if args.verbose:
        cmd.append("-v")
    return cmd


def run_command(cmd: list[str], cwd: Path) -> int:
    print(f"$ {' '.join(cmd)}")
    process = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert process.stdout is not None
    for raw_line in iter(process.stdout.readline, b""):
        if not raw_line:
            break
        text = raw_line.decode("utf-8", errors="replace")
        print(text, end="")
    process.wait()
    return process.returncode


def cmd_detect(args) -> int:
    config, error = resolve_builder_config(args)
    if error or config is None:
        category = "ambiguous-context" if error and "Multiple" in error else "environment-missing"
        return print_failure("EIDE flash context detection failed", category, error)
    uploader, config_map, upload_error = load_upload_config(config.root_dir)
    artifacts = scan_artifacts(config)
    print(f"builder.params: {config.params_path}")
    print(f"target: {config.target}")
    print(f"out_dir: {config.out_dir}")
    print(f"uploader: {uploader or 'unknown'}")
    if upload_error:
        return print_failure("EIDE flash context detection failed", "environment-missing", upload_error)
    section = config_map.get(uploader, {}) if uploader else {}
    if isinstance(section, dict):
        cpu_info = section.get("cpuInfo", {})
        if isinstance(cpu_info, dict) and cpu_info.get("cpuName"):
            print(f"device: {cpu_info['cpuName']}")
        if "speed" in section:
            print(f"speed: {section['speed']}")
    if artifacts:
        primary = artifacts[0]
        print(f"artifact: {primary.path} ({primary.kind}, {primary.size} bytes)")
    else:
        print("artifact: none")
    print_success("EIDE flash context detected")
    return 0


def cmd_scan(args) -> int:
    workspace = Path(args.workspace or os.getcwd())
    matches = scan_builder_params(workspace)
    if not matches:
        return print_failure("No builder.params candidates were found", "environment-missing",
                             f"no builder.params found under {workspace}")
    print("builder.params candidates:")
    for index, item in enumerate(matches, 1):
        config = load_builder_config(item)
        print(f"  {index}. {item} -> target={config.target}, out_dir={config.out_dir}")
    print_success("EIDE builder.params scan completed")
    return 0


def cmd_flash(args) -> int:
    if not FLASH_JLINK_SCRIPT.is_file():
        return print_failure("EIDE flash could not start", "environment-missing",
                             f"flash-jlink script not found: {FLASH_JLINK_SCRIPT}")

    config, error = resolve_builder_config(args)
    if error or config is None:
        category = "ambiguous-context" if error and "Multiple" in error else "environment-missing"
        return print_failure("EIDE flash could not start", category, error)

    uploader, config_map, upload_error = load_upload_config(config.root_dir)
    if upload_error:
        return print_failure("EIDE flash could not start", "environment-missing", upload_error)
    if uploader != "JLink":
        return print_failure("EIDE flash could not start", "project-config-error",
                             f"uploader {uploader} is not supported yet")

    artifact_path = Path(args.artifact).resolve() if args.artifact else None
    if artifact_path is None:
        artifacts = scan_artifacts(config)
        if not artifacts:
            return print_failure("EIDE flash could not start", "artifact-missing",
                                 f"no artifact found under {config.out_dir}")
        artifact_path = artifacts[0].path.resolve()
    elif not artifact_path.is_file():
        return print_failure("EIDE flash could not start", "artifact-missing",
                             f"artifact not found: {artifact_path}")

    upload_section = config_map.get("JLink", {})
    if not isinstance(upload_section, dict):
        return print_failure("EIDE flash could not start", "project-config-error",
                             "JLink upload config is invalid")

    print(f"builder.params: {config.params_path}")
    print(f"uploader: {uploader}")
    print(f"artifact: {artifact_path}")
    cpu_info = upload_section.get("cpuInfo", {}) if isinstance(upload_section, dict) else {}
    device = args.device or (cpu_info.get("cpuName") if isinstance(cpu_info, dict) else None)
    if device:
        print(f"device: {device}")
    interface = args.interface or JLINK_INTERFACE_MAP.get(upload_section.get("proType"), "SWD")
    print(f"interface: {interface}")
    speed = args.speed if args.speed is not None else upload_section.get("speed")
    if speed is not None:
        print(f"speed: {speed}")

    try:
        cmd = build_jlink_command(args, artifact_path, upload_section)
    except RuntimeError as exc:
        return print_failure("EIDE flash could not start", "project-config-error", str(exc))

    result = run_command(cmd, config.root_dir)
    if result == 0:
        print_success("EIDE flash succeeded")
        return 0
    print(f"delegate: {FLASH_JLINK_SCRIPT}")
    return print_failure("EIDE flash failed", "connection-failure",
                         "See delegated flash-jlink output above")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EIDE flash helper")
    parser.add_argument("--workspace", help="Workspace directory")
    parser.add_argument("--builder-params", help="Explicit builder.params path")
    parser.add_argument("--artifact", help="Explicit artifact path")
    parser.add_argument("--device", help="Explicit J-Link device name")
    parser.add_argument("--interface", choices=["SWD", "JTAG"], help="Explicit J-Link interface")
    parser.add_argument("--speed", type=int, help="Explicit J-Link speed kHz")
    parser.add_argument("--base-address", help="Explicit base address for BIN flashing")
    parser.add_argument("--detect", action="store_true", help="Detect current EIDE flash context")
    parser.add_argument("--scan", action="store_true", help="Scan builder.params candidates")
    parser.add_argument("--flash", action="store_true", help="Run flashing")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose passthrough")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.detect:
        return cmd_detect(args)
    if args.scan:
        return cmd_scan(args)
    if args.flash:
        return cmd_flash(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())