"""Build the desktop package on its native OS from a clean source snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

SOURCE_DIR = Path(__file__).resolve().parent
ROOT_FILES = {
    "backend.py", "desktop.py", "pricing.py", "build_desktop.py",
    "CodexTokenDesktop.spec", "build-portable.ps1", "build-macos.sh",
    "requirements-desktop.txt", "requirements-build.txt", "VERSION", "LICENSE",
    "README.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "providers.json.example",
    "便携版使用说明.txt", "启动 Codex Token 看板.cmd", "start-dashboard.cmd",
    "start-dashboard.ps1", "start-dashboard.sh", "start-desktop.command",
    ".gitignore", ".gitattributes",
}
SOURCE_TREES = ("tests", "web", "assets", ".github/workflows")
EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "release", "dist", "build", "work", "outputs"}
RUNTIME_FILES = ("backend.py", "desktop.py", "pricing.py", "VERSION", "requirements-desktop.txt")
PORTABLE_FILES = (
    "LICENSE", "THIRD_PARTY_NOTICES.md", "providers.json.example", "VERSION",
    "便携版使用说明.txt", "启动 Codex Token 看板.cmd",
)


def eligible_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return (
        not path.is_symlink()
        and not any(part in EXCLUDED_DIRS for part in relative.parts)
        and "sqlite" not in path.name.lower()
        and path.name != "providers.json"
        and path.suffix.lower() not in {".pyc", ".pyo"}
    )


def stage_source(source: Path, destination: Path) -> None:
    """Copy only distributable source files; never copy user caches/config."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in sorted(ROOT_FILES):
        path = source / name
        if path.is_file() and eligible_file(path, source):
            shutil.copy2(path, destination / name)
    for name in SOURCE_TREES:
        tree = source / name
        if not tree.is_dir() or tree.is_symlink():
            continue
        for path in sorted(tree.rglob("*")):
            if path.is_file() and eligible_file(path, source):
                if any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents):
                    continue
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)


def source_digest(source: Path) -> str:
    """Hash stable relative paths and original bytes, with unambiguous framing."""
    names = list(RUNTIME_FILES)
    names.extend(path.relative_to(source).as_posix() for path in (source / "web").rglob("*") if path.is_file() and eligible_file(path, source))
    digest = hashlib.sha256()
    for name in sorted(names):
        encoded_name = name.encode("utf-8")
        content = (source / name).read_bytes()
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print("Running: " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def make_macos_icon(stage: Path, build_root: Path) -> None:
    iconset = build_root / "app-icon.iconset"
    iconset.mkdir()
    png = stage / "assets" / "app-icon.png"
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = str(size * scale)
            suffix = "@2x" if scale == 2 else ""
            target = iconset / f"icon_{size}x{size}{suffix}.png"
            run(["sips", "-z", pixels, pixels, str(png), "--out", str(target)], stage)
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(stage / "assets" / "app-icon.icns")], stage)


def file_manifest(package: Path) -> list[dict[str, str]]:
    records = []
    for path in sorted(package.rglob("*"), key=lambda value: value.relative_to(package).as_posix()):
        name = path.relative_to(package).as_posix()
        if name == "build-manifest.json":
            continue
        if path.is_symlink():
            records.append({"path": name, "type": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            records.append({"path": name, "sha256": digest.hexdigest()})
    return records


def build(output_dir: Path, *, skip_tests: bool = False) -> tuple[Path, Path]:
    if sys.platform not in {"win32", "darwin"}:
        raise RuntimeError("Desktop packaging requires a native Windows or macOS host.")
    architecture = platform.machine().lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(architecture, architecture)
    if sys.platform == "darwin" and architecture != "arm64":
        raise RuntimeError("The macOS package requires a native Apple Silicon (arm64) Python runtime.")
    output_dir = output_dir.expanduser().resolve()
    version = (SOURCE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    if not version or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_" for char in version):
        raise RuntimeError("VERSION contains characters unsuitable for a package filename.")
    os_label = "Windows-Portable" if sys.platform == "win32" else "macOS"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    build_root = Path(tempfile.mkdtemp(prefix=f"build-{version}-{stamp}-", dir=output_dir))
    stage = build_root / "source"
    stage_source(SOURCE_DIR, stage)
    runtime_digest = source_digest(stage)
    # Tests can invoke launcher doctor or DesktopRuntime; isolate all state.
    test_env = os.environ.copy()
    test_env["PYTHONDONTWRITEBYTECODE"] = "1"
    test_env["CODEX_DASHBOARD_PYTHON"] = sys.executable
    test_env["LOCALAPPDATA"] = str(build_root / "test-localappdata")
    test_env["HOME"] = str(build_root / "test-home")
    test_env["USERPROFILE"] = test_env["HOME"]
    test_env["CODEX_HOME"] = str(build_root / "test-codex-home")
    for name in ("HOME", "LOCALAPPDATA", "CODEX_HOME"):
        Path(test_env[name]).mkdir(parents=True, exist_ok=True)
    for name in ("sessions", "archived_sessions"):
        (Path(test_env["CODEX_HOME"]) / name).mkdir(parents=True, exist_ok=True)
    if not skip_tests:
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], stage, test_env)
        run(["node", "--check", "web/app.js"], stage, test_env)
    if sys.platform == "darwin":
        make_macos_icon(stage, build_root)
    build_env = os.environ.copy()
    build_env["PYTHONDONTWRITEBYTECODE"] = "1"
    if sys.platform == "darwin":
        build_env.setdefault("MACOSX_DEPLOYMENT_TARGET", "14.0")
    run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--workpath", str(build_root / "work"), "--distpath", str(build_root / "dist"),
        "CodexTokenDesktop.spec",
    ], stage, build_env)
    package = build_root / "dist" / ("CodexTokenDesktop" if sys.platform == "win32" else "CodexTokenDesktop.app")
    if not package.is_dir():
        raise RuntimeError(f"PyInstaller did not produce the expected package: {package}")
    if sys.platform == "win32":
        for name in PORTABLE_FILES:
            shutil.copy2(stage / name, package / name)
    manifest = {
        "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform,
        "architecture": architecture,
        "python_version": platform.python_version(),
        "source_digest": runtime_digest,
        "source_digest_algorithm": "sha256:path-length/path/content-length/content;sorted-posix-paths;raw-bytes",
        "source_commit": os.environ.get("GITHUB_SHA") or None,
        "validation": "skipped" if skip_tests else "unittest-and-node-check-passed",
        "files": file_manifest(package),
    }
    suffix = build_root.name.rsplit("-", 1)[-1]
    stem = f"CodexTokenDashboard-{os_label}-{architecture}-{version}-{stamp}-{suffix}"
    archive = output_dir / f"{stem}.zip"
    manifest_path = output_dir / f"{stem}.build-manifest.json"
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    if sys.platform == "win32":
        (package / "build-manifest.json").write_text(manifest_text, encoding="utf-8")
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as handle:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    handle.write(path, path.relative_to(package.parent).as_posix())
    else:
        # Leave the manifest outside the signed .app; ditto preserves symlinks.
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(package), str(archive)], stage)
    print(f"Archive: {archive}", flush=True)
    print(f"Manifest: {manifest_path}", flush=True)
    print(f"Source digest: {runtime_digest}", flush=True)
    return archive, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=SOURCE_DIR / "release")
    parser.add_argument("--skip-tests", action="store_true", help="Skip checks only when this source was already validated.")
    args = parser.parse_args()
    try:
        build(args.output_dir, skip_tests=args.skip_tests)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
