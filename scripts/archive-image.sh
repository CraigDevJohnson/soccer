#!/usr/bin/env bash
set -euo pipefail
registry="$ARCHIVE_ACCOUNT.dkr.ecr.us-west-2.amazonaws.com"
repository="soccer-archive-$TARGET-worker"
aws ecr get-login-password | docker login --username AWS --password-stdin "$registry"
if [[ "$TARGET" == prod ]]; then
    dev_image=$(aws lambda get-function --function-name soccer-archive-dev-worker --query Code.ResolvedImageUri --output text)
    digest=${dev_image##*@}
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ && "$dev_image" == "$registry/soccer-archive-dev-worker@$digest" ]]
    tag="verified-${digest#sha256:}"
else
    tag="git-$GITHUB_SHA"
fi
# Tags are immutable. A retry after a successful push can report the same digest.
lookup=$(aws ecr batch-get-image --repository-name "$repository" --image-ids "imageTag=$tag" --query 'images[0].imageId.imageDigest' --output text)
if [[ "$lookup" == None ]]; then
    if [[ "$TARGET" == prod ]]; then
        docker pull "$dev_image"
        docker tag "$dev_image" "$registry/$repository:$tag"
        docker push "$registry/$repository:$tag"
    else
        docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
            --build-arg "ARCHIVE_SOURCE_REVISION=$GITHUB_SHA" \
            --file scripts/boise_collect/Dockerfile.archive --tag "$registry/$repository:$tag" --push .
    fi
    lookup=$(aws ecr describe-images --repository-name "$repository" --image-ids "imageTag=$tag" --query 'imageDetails[0].imageDigest' --output text)
fi
[[ "$lookup" =~ ^sha256:[0-9a-f]{64}$ ]]
if [[ "$TARGET" == prod ]]; then
    test "$lookup" = "$digest"
fi
echo "Published $repository@$lookup" >> "$GITHUB_STEP_SUMMARY"
