# Mounted run storage with a local cache

TriCycle can use a normal local `runs/` directory or a symbolic link to a mounted
remote directory. Storage selection is a host setup choice; model, sampling,
and analysis code keep their existing filesystem interfaces. A checkout without
this setup continues to use local storage. Explicit `run_dir` and `out_dir`
values still take precedence.

## Host setup

Configure an authenticated rclone remote outside the repository. Keep credentials,
mount paths, service definitions, and cache settings in host-local configuration.
Use a separate local cache on each device. Do not run overlapping rclone mounts
against the same VFS cache directory.

On macOS, an administrator or user can run an NFS mount with full VFS caching.
For example, in a separate terminal:

```sh
export TRICYCLE_NAS_MOUNT="$HOME/mnt/tricycle-runs"
export TRICYCLE_NAS_CACHE="$HOME/Library/Caches/tricycle-runs"
mkdir -p "$TRICYCLE_NAS_MOUNT" "$TRICYCLE_NAS_CACHE"
rclone nfsmount nas:cold-data/tricycle-data "$TRICYCLE_NAS_MOUNT" \
  --cache-dir "$TRICYCLE_NAS_CACHE" \
  --vfs-cache-mode full \
  --vfs-cache-max-size 200G \
  --vfs-cache-min-free-space 100G \
  --vfs-cache-max-age 168h \
  --vfs-write-back 5s \
  --dir-cache-time 30s \
  --vfs-links \
  --option locallocks
```

Replace `nas:` with the configured remote. Install the command as a host service
for use across logins; the mount must be ready before experiment supervisors start.
Keep launchd stdout/stderr destinations on local disk under `.tricycle/`; launchd
must be able to open its service logs before the Python process starts. Remote
log destinations can prevent service startup even when interactive access works.
On Linux use the supported `rclone mount` setup instead. Rclone and filesystem
mount prerequisites belong to host setup, not the project's Python dependencies.

After confirming that the mounted directory contains the intended run collection,
connect a new checkout from its repository root. This refuses an existing path:

```sh
export TRICYCLE_NAS_MOUNT="$HOME/mnt/tricycle-runs"
python3.11 - "$TRICYCLE_NAS_MOUNT" <<'PY'
import os
import sys
from pathlib import Path

target = Path(sys.argv[1]).absolute()
source = Path.cwd() / "runs"
if not os.path.ismount(target) or not target.is_dir():
    raise SystemExit("The configured run storage must be mounted first")
if source.exists() or source.is_symlink():
    raise SystemExit("Existing runs retained: reconcile and back up before switching")
source.symlink_to(target, target_is_directory=True)
PY
```

When using a shared mount of the entire NAS, validate that mount separately and
link to its run-data subdirectory. Before switching an existing checkout, stop
local users of the directory, reconcile missing or changed files, retain a local
rollback copy, and then replace `runs` with the link. Do not use a recursive delete
to replace an existing link. Do not keep a FreeFileSync job whose source `runs/`
now resolves to the same remote directory as its destination.

## Cache and write behavior

Full VFS mode buffers reads and writes on local disk. Closing a file does not
prove the NAS has received it: upload occurs after the configured write-back
delay. Before shutdown, unmounting, or another device taking over a task, verify
that the mount has no queued or active uploads. Never manually delete the VFS
cache, including during upload failures; it may contain the only copy of new data.

Cache size and minimum free space are cleanup targets, not strict bounds. Open
files and pending writes can keep usage above those targets. NAS metadata lookups,
uncached reads, and publication still require connectivity; this is not an
offline-first execution system. Mount startup failures must not silently redirect
writes into an ordinary local mountpoint directory. Host setup can protect an
empty, unmounted mountpoint from writes; never change the permissions of the
mounted remote root to achieve that protection.

Use one writer per run across devices. VFS caching is not a distributed locking
or transaction system, and another device's changes may remain hidden until
directory and attribute caches refresh. Validate the required create, seek,
rename, and local process-lock behavior on the actual mount before live runs.
On macOS, `locallocks` enables NFS client-side locking for the project's `flock`
users; a default mount can fail with `No locks available`. These locks protect
processes on that client only, not writers on another device. Do not remove
application locks to work around an incompatible mount.
Use a fresh temporary probe outside the run ledger and verify uploaded bytes
through an independent remote read, rather than reading only from the cache.

## Historical archives and portability

Existing `.tar.zst` files remain archives. A symbolic link and VFS cache do not
automatically expose their members as original directories. Restore a selected
archive before a command that expects the original record path, retaining its
checksum and avoiding overwrites of newer records.

Mounting `runs/` does not make an experiment self-contained. Preserve referenced
shared data, model assets, and the configured asset store separately. Existing
records may contain absolute paths from their original host. Other devices need
compatible paths or explicit supported relocation; mounting alone does not
rewrite records, credentials, or immutable hashes.

See [run record scope](run-records.md),
[rclone VFS caching](https://rclone.org/commands/rclone_mount/#vfs-file-caching),
and [rclone NFS mount](https://rclone.org/commands/rclone_nfsmount/).
