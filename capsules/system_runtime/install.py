"""Install only application code; all dependencies belong to system Python.

This deployment capsule does not install packages or access application data.
Stop the service before replacing an existing runtime.
"""
from pathlib import Path
import argparse
import importlib
import importlib.metadata
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile


def install(source: Path) -> None:
    if os.geteuid() != 0 or sys.prefix != sys.base_prefix:
        raise RuntimeError("run this installer as root with system Python")
    sys.path.extend(site.getsitepackages(["/usr/local"]))
    required = {"playwright": "playwright", "secretstorage": "SecretStorage",
                "yt_dlp": "yt-dlp"}
    versions = {}
    for module, distribution in required.items():
        loaded = importlib.import_module(module)
        if not str(loaded.__file__).startswith(("/usr/lib", "/usr/local/lib")):
            raise RuntimeError("dependency must be installed globally: " + module)
        versions[distribution] = importlib.metadata.version(distribution)
    source = source.resolve(strict=True)
    destination = Path("/usr/local/libexec/x-repost-downloader")
    launcher = Path("/usr/local/bin/x-repost-downloader")
    if destination.is_symlink() or launcher.is_symlink():
        raise RuntimeError("refusing to replace a symbolic link")
    if destination.exists() and not (destination / "runtime-manifest.json").is_file():
        raise RuntimeError("destination is not a managed application runtime")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".xrd-install-", dir=destination.parent))
    try:
        package = source / "src/x_repost_downloader"
        if any(p.is_symlink() for p in package.rglob("*")):
            raise RuntimeError("application source contains symbolic links")
        shutil.copytree(package, stage / "x_repost_downloader",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        (stage / "runtime-manifest.json").write_text(json.dumps({
            "interpreter": "/usr/bin/python3", "source_commit": commit,
            "global_dependencies": versions, "application_code_only": True,
        }, indent=2) + "\n")
        for path in [stage, *stage.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
            os.chown(path, 0, 0)
        # Managed runtime is reproducible code, never state or credentials.
        # Remove superseded copies rather than retain private dependencies.
        if destination.exists():
            shutil.rmtree(destination)
        os.rename(stage, destination)
        subprocess.run(["restorecon", "-R", str(destination)], check=True)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        temporary = launcher.with_name(".x-repost-downloader.install")
        with temporary.open("x") as handle:
            handle.write(Path(__file__).with_name("launcher.py").read_text())
        temporary.chmod(0o755)
        os.chown(temporary, 0, 0)
        os.replace(temporary, launcher)
        subprocess.run(["restorecon", str(launcher)], check=True)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    install(args.source)
