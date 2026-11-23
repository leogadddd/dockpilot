"""Read-only preview of removable-drive jobs; no upload or mount operations."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

MEDIA_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp",
    ".gif", ".tif", ".tiff", ".dng", ".raw", ".cr2", ".nef",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp",
})
ALLOWED_ACTIONS = frozenset({"immich.import"})


class ValidationError(Exception):
    """The supplied manifest or local configuration failed validation."""


def read_yaml(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ValidationError(f"Missing or unsafe YAML file: {path}")
    obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or obj.get("version") != 1:
        raise ValidationError(f"Expected version: 1 YAML mapping: {path}")
    return obj


def safe_source(drive: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Source must be a relative directory")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise ValidationError("Source must stay within the drive")
    path = drive
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValidationError("Symlink encountered in source path")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(drive) or not resolved.is_dir():
        raise ValidationError("Source is not a directory within the drive")
    return resolved


def count_media(folder: Path) -> tuple[int, int]:
    count = size = 0
    for root, directories, files in os.walk(folder, followlinks=False):
        directories[:] = [
            d for d in directories
            if not (Path(root) / d).is_symlink()
        ]
        for filename in files:
            path = Path(root) / filename
            if path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                count += 1
                size += info.st_size
            except OSError as exc:
                raise ValidationError(f"Cannot inspect {path}: {exc}") from exc
    return count, size


def preview(drive_arg: str, config_arg: str) -> dict:
    drive = Path(drive_arg).resolve(strict=True)
    if not drive.is_dir() or drive == Path("/"):
        raise ValidationError("Drive must be a mounted directory other than /")
    # Avoid unexpectedly scanning the system disk if removable drive isn't mounted.
    if not drive.is_mount():
        raise ValidationError("Drive path must be a mount point")
    manifest = read_yaml(drive / "homelab.yaml")
    config = read_yaml(Path(config_arg))
    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise ValidationError("Server targets mapping is required")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list):
        raise ValidationError("Drive jobs must be a list")
    results = []
    seen_ids: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValidationError("Each job must be a mapping")
        if job.get("enabled", True) is False:
            continue
        job_id, action, target = job.get("id"), job.get("action"), job.get("target")
        if not isinstance(job_id, str) or not job_id or job_id in seen_ids:
            raise ValidationError("Jobs require distinct nonempty string IDs")
        seen_ids.add(job_id)
        if action not in ALLOWED_ACTIONS:
            raise ValidationError(f"Action not supported in preview: {action}")
        target_cfg = targets.get(target)
        if not isinstance(target_cfg, dict) or action not in target_cfg.get("allowed_actions", []):
            raise ValidationError(f"Action {action} not approved for target: {target}")
        if target_cfg.get("type") != "immich":
            raise ValidationError(f"Unexpected target type: {target}")
        if job.get("mode") != "copy":
            raise ValidationError("Only non-destructive copy mode is supported")
        if job.get("trigger") != "on-connect":
            raise ValidationError("Only on-connect manifests are previewed")
        options = job.get("options", {})
        if not isinstance(options, dict) or options.get("preserve_originals") is not True:
            raise ValidationError("preserve_originals must be true")
        source = safe_source(drive, job.get("source"))
        count, size = count_media(source)
        results.append({
            "id": job_id,
            "action": action,
            "target": target,
            "source": str(source.relative_to(drive)),
            "media_files": count,
            "bytes": size,
        })
    return {"mode": "dry-run", "drive_name": manifest.get("name", "Unnamed"), "jobs": results}


def notify_ntfy(config: dict, title: str, message: str) -> None:
    settings = config.get("notifications", {})
    if not isinstance(settings, dict) or settings.get("provider") != "ntfy":
        raise ValidationError("ntfy notifications are not configured")
    url = settings.get("server")
    topic = settings.get("topic")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValidationError("ntfy server must use HTTPS")
    if not isinstance(topic, str) or not topic or "/" in topic:
        raise ValidationError("Invalid ntfy topic")
    req = urllib.request.Request(
        url.rstrip("/") + "/" + topic,
        data=message.encode(),
        headers={"Title": title, "Content-Type": "text/plain; charset=utf-8"},
        method="POST",
    )
    user, password = os.environ.get("NTFY_USERNAME"), os.environ.get("NTFY_PASSWORD")
    if user and password:
        import base64
        encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {encoded}")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise ValidationError(f"ntfy HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValidationError(f"ntfy notification failed: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dockpilot manifest dry run (never imports)")
    parser.add_argument("--drive", required=True, help="Read-only mounted USB directory")
    parser.add_argument("--config", default="/etc/dockpilot/config.yaml")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--notify", action="store_true", help="Send dry-run alerts via ntfy")
    args = parser.parse_args(argv)
    try:
        config = read_yaml(Path(args.config))
        if args.notify:
            notify_ntfy(config, "Dockpilot preview started", "Scanning a USB manifest (dry run only)")
        result = preview(args.drive, args.config)
        print(json.dumps(result, indent=2) if args.json else (
            f"Dockpilot DRY RUN: {result['drive_name']}\n" +
            "\n".join(
                f"{j['id']}: {j['media_files']:,} files / {j['bytes'] / 1024**3:.2f} GiB -> {j['target']}"
                for j in result["jobs"]
            ) + "\nNo files were imported or modified."
        ))
        if args.notify:
            notify_ntfy(config, "Dockpilot preview complete", f"Validated {len(result['jobs'])} jobs; no import performed")
        return 0
    except (ValidationError, OSError, yaml.YAMLError, ValueError) as exc:
        print(f"Dockpilot error: {exc}", file=sys.stderr)
        if args.notify:
            try:
                config = read_yaml(Path(args.config))
                notify_ntfy(config, "Dockpilot preview failed", str(exc))
            except Exception:
                pass
        return 1
