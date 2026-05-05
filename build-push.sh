#!/usr/bin/env bash
# Build both Docker images and push them to Docker Hub.
# Run this from the repo root: ./build-push.sh
set -euo pipefail

# Load variables from .env
set -a && source .env && set +a

BACKEND_IMAGE="${DOCKERHUB_USERNAME}/correctexam-back:${IMAGE_TAG}"
FRONTEND_IMAGE="${DOCKERHUB_USERNAME}/correctexam-front:${IMAGE_TAG}"

echo "==> Building backend:  $BACKEND_IMAGE"
docker build -t "$BACKEND_IMAGE" ./corrigeExamBack

echo "==> Building frontend: $FRONTEND_IMAGE"
docker build -t "$FRONTEND_IMAGE" ./corrigeExamFront

echo "==> Pushing to Docker Hub..."
docker push "$BACKEND_IMAGE"
docker push "$FRONTEND_IMAGE"

echo ""
echo "Published images:"
echo "  docker pull $BACKEND_IMAGE"
echo "  docker pull $FRONTEND_IMAGE"
