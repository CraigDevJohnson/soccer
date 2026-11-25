#!/usr/bin/env bash
set -euo pipefail
registry="$ARCHIVE_ACCOUNT.dkr.ecr.us-west-2.amazonaws.com"
worker="soccer-archive-$TARGET-worker"
image="$registry/$worker@$DIGEST"
if [[ "$TARGET" == prod ]]; then
    dev=$(aws lambda get-function --function-name soccer-archive-dev-worker --query Code.ResolvedImageUri --output text)
    test "$dev" = "$registry/soccer-archive-dev-worker@$DIGEST"
fi
revision=$(aws lambda get-function-configuration --function-name "$worker" --query RevisionId --output text)
aws lambda update-function-code --function-name "$worker" --image-uri "$image" --revision-id "$revision" --query LastUpdateStatus
aws lambda wait function-updated-v2 --function-name "$worker"
actual=$(aws lambda get-function --function-name "$worker" --query Code.ResolvedImageUri --output text)
test "$actual" = "$image"
echo "Released $actual" >> "$GITHUB_STEP_SUMMARY"
