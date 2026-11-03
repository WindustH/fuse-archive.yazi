#!/usr/bin/env bash

# check if any other yazi instance is still running
if [ "$(pgrep -c yazi)" -le 1 ]; then
  # get mount points
  fuse_archive_mnt_points=$(findmnt --output TARGET --noheadings --list | grep "^${1}/yazi/fuse-archive" | sort -r)
  # Yazi's queued directory change and nested archives can keep a mount busy.
  # Detach busy read-only mounts so they are released when those handles close.
  while IFS= read -r mnt_point; do
    if [[ -n "$mnt_point" ]]; then
      umount "$mnt_point" 2>/dev/null || umount -l "$mnt_point"
    fi
  done <<<"$fuse_archive_mnt_points"
fi
