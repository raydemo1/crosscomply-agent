#!/bin/sh
# Start SeaweedFS as the S3-compatible store for material originals and PDF
# reports. The S3 identity file is generated from the environment so the
# secret never lands in the repository or the image.
set -eu

: "${S3_ACCESS_KEY:?Set S3_ACCESS_KEY in .env}"
: "${S3_SECRET_KEY:?Set S3_SECRET_KEY in .env}"
case "$S3_ACCESS_KEY$S3_SECRET_KEY" in
  *'"'* | *'\'*) echo "S3_ACCESS_KEY / S3_SECRET_KEY must not contain quotes or backslashes" >&2; exit 1 ;;
esac

umask 077
cat > /tmp/s3.json <<EOF
{"identities": [{"name": "crosscomply",
  "credentials": [{"accessKey": "$S3_ACCESS_KEY", "secretKey": "$S3_SECRET_KEY"}],
  "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}
EOF

# Without the volume size caps SeaweedFS preallocates large volumes and can
# fill a small disk; total capacity is roughly VOLUME_MAX x VOLUME_SIZE_LIMIT_MB.
exec weed server -dir=/data -s3 -s3.port=8333 -s3.config=/tmp/s3.json \
  -master.volumeSizeLimitMB="${SEAWEEDFS_VOLUME_SIZE_LIMIT_MB:-64}" \
  -master.volumePreallocate=false \
  -volume.max="${SEAWEEDFS_VOLUME_MAX:-20}"
