# Shared by run-backup.sh and run-restore.sh: configures rclone's "objstore"
# remote from the OBJECT_STORAGE_* env vars the operator sets (see
# resources/backup.go and resources/restore.go's envFromSecret calls).
# Uses rclone's env-var config form (no config file needed, nothing
# persisted) with PROVIDER=Other, which targets any S3-compatible endpoint
# (MinIO, DigitalOcean Spaces, etc.), not just AWS itself.
_rclone_configure_remote() {
  export RCLONE_CONFIG_OBJSTORE_TYPE=s3
  export RCLONE_CONFIG_OBJSTORE_PROVIDER=Other
  export RCLONE_CONFIG_OBJSTORE_ENV_AUTH=false
  export RCLONE_CONFIG_OBJSTORE_ENDPOINT="$OBJECT_STORAGE_ENDPOINT"
  export RCLONE_CONFIG_OBJSTORE_ACCESS_KEY_ID="$OBJECT_STORAGE_ACCESS_KEY"
  export RCLONE_CONFIG_OBJSTORE_SECRET_ACCESS_KEY="$OBJECT_STORAGE_SECRET_KEY"
}
