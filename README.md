# Dockpilot

Configuration-driven removable-drive workflows for self-hosted homelabs.

**Current status: safe preview / dry-run (v0.1).** Dockpilot can read a `homelab.yaml` manifest from a mounted removable drive, validate job definitions against a server-side allowlist, count eligible Immich media assets, and optionally notify an authenticated ntfy server. **It does not mount disks, auto-run on USB insertion, upload files, or perform backups yet.**

## Motivation

Instead of building a separate script for every USB drive, add a `homelab.yaml` file at the root of a drive. Jobs refer to logical targets configured on the host. API keys and passwords stay on the host, never on the portable drive.

## Install on Debian / Proxmox

Requires Python 3.11+ and PyYAML.

```bash
sudo apt update
sudo apt install -y python3 python3-yaml
git clone https://github.com/leogadddd/dockpilot.git
cd dockpilot
sudo install -d -m 700 /etc/dockpilot
sudo cp config/example.yaml /etc/dockpilot/config.yaml
sudo chmod 600 /etc/dockpilot/config.yaml
```

Edit the target aliases in `/etc/dockpilot/config.yaml` to match your drive's `homelab.yaml`. The example URL is illustrative and must be replaced before implementing uploads.

## Preview an existing drive

**Mount the drive read-only** before scanning; verify the mount is the intended drive.

```bash
sudo mkdir -p /mnt/dockpilot-drive
sudo mount -o ro /dev/disk/by-uuid/YOUR-UUID /mnt/dockpilot-drive
python3 -m dockpilot --drive /mnt/dockpilot-drive --config /etc/dockpilot/config.yaml
```

From the repo directory, Python will find the local `dockpilot` package.

Use `--json` for machine-readable results. Use `--notify` to send start/success/failure messages through ntfy; configured credentials are supplied in environment variables (not committed to Git). Example:

```bash
export NTFY_USERNAME='your-publisher-user'
read -rsp 'ntfy password: ' NTFY_PASSWORD; echo
export NTFY_PASSWORD
python3 -m dockpilot --drive /mnt/dockpilot-drive --config /etc/dockpilot/config.yaml --notify
unset NTFY_PASSWORD
```

When done: `sudo umount /mnt/dockpilot-drive`. Never mount a drive with a running application writing to it in parallel.

## Manifest

See [examples/immich-import/homelab.yaml](examples/immich-import/homelab.yaml). Supported action syntax is `immich.import`; `immich.backup` is reserved for future releases. Jobs **only preview** their inputs in v0.1. The host allowlist must explicitly approve each target/action pair.

`source` is a path **relative to the USB drive root**, not the host or the Immich VM. Absolute paths, parent traversal and symlinks are rejected. The agent never executes shell commands from a drive manifest.

## Safety and limitations

- Drive manifests are **untrusted input**. Only allowlisted actions and targets are accepted.
- A manifest file alone never initiates an upload; auto-detection and execution are not installed by this release.
- No API keys, tokens, LAN addresses, photos, logs or backup data should be committed.
- Duplicate skipping is a requested future import feature, **not a guarantee** of v0.1.
- Immich's database and assets require separate, consistent backups. A photo import is not a backup.
- The dry run counts extensions, not proven decodable media; exFAT times and metadata can be imperfect.

## Roadmap

1. Verified USB mount and udev-to-systemd event handling with trusted-device approval.
2. Staging to persistent storage with checksums, space checks and resumability.
3. Upload worker located in the Debian media VM, with Immich API credentials stored only there.
4. Idempotent import journal, structured progress notifications and safe retries.
5. Database + media backup action, retention policy and restore validation.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

License: MIT.
