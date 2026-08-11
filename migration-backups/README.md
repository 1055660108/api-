# Encrypted migration backups

This directory stores encrypted core migration snapshots only. The decryption key must remain outside Git.

Each core archive contains:

- Full PostgreSQL custom dump, including users, account pools, tasks, quotas and settings.
- Redis snapshot.
- Production Compose configuration and environment file.
- Version, Git commit, container and volume metadata.
- Restore notes and checksums.

The full application-volume backup, browser profiles and source archive are stored in the local offline backup directory because they are too large for Git.

Decrypt an archive with:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -pass file:DECRYPTION_KEY.txt -in BACKUP.tar.enc -out BACKUP.tar
```

For a complete server restore, use `scripts/restore_server_migration.sh` with the full encrypted backup and its separate key file.
