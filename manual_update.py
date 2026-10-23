"""Prepare a separate, reviewable installation; never replace the working folder.

Use an official package and independently obtained SHA-256. Stop the sender
before preparing a consistent application-level snapshot. No Telegram calls.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import zipfile

RUNTIME = {
    "ai.py", "arbitration_api.py", "bot.py", "config.py", "database.py",
    "launcher.py", "main.py", "telegram_client.py", "updater.py",
    "access_lease.py", "access_runtime.py", "context_adapters.py", "product_catalog.py",
    "start.py", "start.bat", "start_mac.sh", "requirements.txt", "products.json",
    "version.txt", ".env.example", "manual_update.py", "MANUAL_UPDATES.md",
    "BUSINESS_PACKAGES.md", "CENTRAL_ACCESS.md",
    "sync_data.py", "sync_protocol.py", "sync_runtime.py", "file_lock.py",
    "manager_setup.py", "DATA_POLICY.md", "schedule_state.py", "platform_updater.py",
    "PILOT.md", "PROJECT_AUDIT.md", "startup_update.py", "transition_setup.py", "STARTUP_UPDATES.md",
}
MAX_BYTES = 64 * 1024 * 1024
MISSING = object()


def allowed_name(name):
    if not isinstance(name, str) or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    if name.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return False
    # Avoid Windows device names and names normalized differently by NTFS.
    for part in parts:
        if part != part.rstrip(" .") or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", part.split(".")[0], re.I):
            return False
    return name in RUNTIME or (len(parts) >= 2 and parts[0] == "prompts" and name.endswith(".md"))


def package_files(root):
    root = Path(root).resolve()
    names = RUNTIME | {p.relative_to(root).as_posix() for p in (root/"prompts").rglob("*.md")}
    for name in names:
        path = root / name
        if not allowed_name(name) or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Missing or unsafe package file: {name}")
    return sorted(names)


def build_package(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    names = package_files(root)
    payload = {name: (root/name).read_bytes() for name in names}
    version = (root/"version.txt").read_text(encoding="utf-8-sig").strip()
    manifest = {"format": 1, "version": version,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never overwrite an existing release artifact.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        for name, data in payload.items():
            archive.writestr(name, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return digest


def verify_package(package, expected_hash):
    package = Path(package)
    if package.stat().st_size > MAX_BYTES:
        raise ValueError("Package exceeds size limit")
    data = package.read_bytes()
    if len(expected_hash) != 64 or hashlib.sha256(data).hexdigest() != expected_hash.lower():
        raise ValueError("Package SHA-256 mismatch")
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(i.file_size for i in entries) > MAX_BYTES:
            raise ValueError("Unpacked package exceeds limits")
        names = [i.filename for i in entries]
        if len(set(n.casefold() for n in names)) != len(names):
            raise ValueError("Duplicate/case-colliding archive entries")
        for info in entries:
            mode = info.external_attr >> 16
            if info.is_dir() or stat.S_ISLNK(mode) or (info.filename != "manifest.json" and not allowed_name(info.filename)):
                raise ValueError(f"Forbidden archive entry: {info.filename}")
        manifest = json.loads(archive.read("manifest.json"))
        if (not isinstance(manifest, dict) or type(manifest.get("format")) is not int
                or manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict)):
            raise ValueError("Unsupported package format")
        files = manifest["files"]
        if set(files) != set(names) - {"manifest.json"} or not RUNTIME.issubset(files):
            raise ValueError("Package manifest is incomplete")
        payload = {}
        for name, digest in files.items():
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f"File checksum mismatch: {name}")
            if name.endswith(".py"):
                compile(content, name, "exec")
            payload[name] = content
        if payload["version.txt"].decode("utf-8-sig").strip() != manifest.get("version"):
            raise ValueError("Package version mismatch")
    return manifest, payload


def git_baseline(root, name):
    # Read repository content only for known runtime/prompts; never credentials.
    if not allowed_name(name) or not (Path(root)/".git").exists():
        return MISSING
    try:
        result = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{name}"],
                                capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return MISSING
    return result.stdout if result.returncode == 0 else MISSING


def same_text(left, right):
    """Git stores LF while a clean Windows checkout can contain CRLF."""
    return left.replace(b"\r\n", b"\n") == right.replace(b"\r\n", b"\n")


def merge_json(base, local, incoming, conflicts, path="products"):
    """Three-way merge. Preserve local choices; report conflicting changes."""
    if local == incoming:
        return local
    if base is not MISSING:
        if local == base:
            return incoming
        if incoming == base:
            return local
    if isinstance(local, dict) and isinstance(incoming, dict) and (base is MISSING or isinstance(base, dict)):
        result = {}
        for key in sorted(set(local) | set(incoming)):
            original = base.get(key, MISSING) if isinstance(base, dict) else MISSING
            left, right = local.get(key, MISSING), incoming.get(key, MISSING)
            if left is MISSING and original is MISSING:
                value = right
            elif right is MISSING and original is MISSING:
                value = left
            else:
                value = merge_json(original, left, right, conflicts, f"{path}.{key}")
            if value is not MISSING:
                result[key] = value
        return result
    conflicts.append(path)
    return local


def registry_products(data):
    if not isinstance(data, dict):
        raise ValueError("Product registry must be an object")
    if "schema_version" in data:
        if data["schema_version"] != 1 or not isinstance(data.get("products"), dict):
            raise ValueError("Unsupported product registry schema")
        return data["products"]
    return data


def snapshot_file(source, destination):
    if source.is_symlink():
        raise ValueError("Refusing to follow local symlink: " + source.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".session"}:
        # SQLite backup also includes committed WAL contents; do not raw-copy DBs.
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old:
            with closing(sqlite3.connect(destination)) as new:
                old.backup(new)
                if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Snapshot database integrity check failed")
        shutil.copystat(source, destination)
    else:
        shutil.copy2(source, destination)


def snapshot_local(source, candidate):
    # Never copy .git, environments, old update archives, or authority keys.
    paths = []
    for path in source.iterdir():
        if path.name == ".env" or path.name.endswith(".session"):
            paths.append(path)
    data = source / "data"
    if data.is_symlink():
        raise ValueError("Local data directory is a symlink")
    if data.exists():
        for directory, dirs, files in os.walk(data, followlinks=False):
            for name in dirs:
                if (Path(directory)/name).is_symlink():
                    raise ValueError("Local data contains a directory symlink")
            if Path(directory) == data:
                dirs[:] = [n for n in dirs if n not in {"backups", "updates", "access_authority"}]
            for name in files:
                if name in {'active-installation.json', 'active-installation.tmp'}:
                    continue
                if name.endswith(("-wal", "-shm", "-journal", ".lock")):
                    continue
                paths.append(Path(directory)/name)
    for path in paths:
        snapshot_file(path, candidate/path.relative_to(source))


def prepare(source, package, expected_hash, destination, baseline_provider=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir() or destination.exists():
        raise ValueError("Source must exist and destination must be new")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Candidate and source must be separate directories")
    manifest, payload = verify_package(package, expected_hash)
    source_version = (source/"version.txt").read_text(encoding="utf-8-sig").strip()
    def version_tuple(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
            raise ValueError("Invalid application version")
        return tuple(int(part) for part in value.split("."))
    if version_tuple(manifest["version"]) <= version_tuple(source_version):
        raise ValueError("Candidate must have a newer version; downgrade/reinstall is not automatic")
    report = {"source_version": source_version,
              "candidate_version": manifest["version"], "preserved": [], "conflicts": [],
              "ready_to_run": False, "dependencies_installed": False,
              "note": "Source unchanged. Review conflicts, configure access, install dependencies and test before use."}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sender-stage-", dir=destination.parent) as temporary:
        candidate = Path(temporary)/"candidate"
        candidate.mkdir()
        for name, incoming in payload.items():
            target = candidate/name
            target.parent.mkdir(parents=True, exist_ok=True)
            local_path = source/name
            if local_path.is_symlink() or not local_path.resolve().is_relative_to(source):
                raise ValueError("Unsafe local path: " + name)
            content = incoming
            if local_path.is_file():
                local = local_path.read_bytes()
                base = baseline_provider(source, name) if baseline_provider else git_baseline(source, name)
                if name == "products.json":
                    original = registry_products(json.loads(base)) if base is not MISSING else MISSING
                    new_registry = json.loads(incoming)
                    merged = merge_json(original, registry_products(json.loads(local)),
                                        registry_products(new_registry), report["conflicts"])
                    if "schema_version" in new_registry:
                        merged = {"schema_version": 1, "products": merged}
                    content = (json.dumps(merged, ensure_ascii=False, indent=2)+"\n").encode()
                    if content != incoming:
                        report["preserved"].append(name)
                elif name.startswith("prompts/") or (base is not MISSING and not same_text(local, base)):
                    if not same_text(local, incoming) and (base is MISSING or not same_text(local, base)):
                        content = local
                        report["preserved"].append(name)
                        if base is MISSING or not same_text(incoming, base):
                            report["conflicts"].append(name)
            target.write_bytes(content)
        # Include draft and custom prompt files absent from the incoming release.
        prompts = source/"prompts"
        if prompts.is_symlink():
            raise ValueError("Local prompts directory is a symlink")
        if prompts.exists():
            for directory, dirs, files in os.walk(prompts, followlinks=False):
                if any((Path(directory)/n).is_symlink() for n in dirs):
                    raise ValueError("Local prompts contain a directory symlink")
                for name in files:
                    path = Path(directory)/name
                    relative = path.relative_to(source).as_posix()
                    if path.suffix == ".md" and relative not in payload:
                        if relative.casefold() in {name.casefold() for name in payload}:
                            raise ValueError("Local prompt differs only by filename case: " + relative)
                        snapshot_file(path, candidate/relative)
                        report["preserved"].append(relative)
        snapshot_local(source, candidate)
        # Preserved local Python modifications must also pass syntax validation.
        for path in candidate.glob("*.py"):
            compile(path.read_bytes(), path.name, "exec")
        (candidate/"start_mac.sh").chmod(0o755)
        (candidate/"update_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.rename(candidate, destination)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build a new-format package; never publishes it")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    prep = sub.add_parser("prepare", help="Prepare a separate folder, leaving the source untouched")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--package", type=Path, required=True)
    prep.add_argument("--sha256", required=True)
    prep.add_argument("--destination", type=Path, required=True)
    prep.add_argument("--sender-stopped", action="store_true", required=True,
                      help="Confirm all senders using the source folder have been stopped")
    args = parser.parse_args(argv)
    if args.command == "build":
        print("SHA-256: " + build_package(args.source, args.output))
        return 0
    report = prepare(args.source, args.package, args.sha256, args.destination)
    print(f"Candidate prepared. Preserved: {len(report['preserved'])}; conflicts: {len(report['conflicts'])}.")
    print("Review update_report.json. This is not an activated or validated installation.")
    return 2 if report["conflicts"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, sqlite3.Error) as exc:
        print("Preparation failed: " + str(exc))
        raise SystemExit(1)
