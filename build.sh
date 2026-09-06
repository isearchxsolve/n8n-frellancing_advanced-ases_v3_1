#!/bin/bash
# ASES Docker Build Script

set -e

IMAGE_NAME="${IMAGE_NAME:-ases}"
TAG="${TAG:-latest}"
BUILD_TARGET="${BUILD_TARGET:-production}"

echo "═══════════════════════════════════════════════════════════════"
echo "  ASES Docker Build"
echo "  Image: ${IMAGE_NAME}:${TAG}"
echo "  Target: ${BUILD_TARGET}"
echo "═══════════════════════════════════════════════════════════════"

# Check prerequisites
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found"
    exit 1
fi

# Build
echo ""
echo "Building Docker image..."
docker build \
    --target "${BUILD_TARGET}" \
    -t "${IMAGE_NAME}:${TAG}" \
    -t "${IMAGE_NAME}:$(date +%Y%m%d)" \
    --build-arg BUILDKIT_INLINE_CACHE=1 \
    --progress=plain \
    .

echo ""
echo "✅ Build complete: ${IMAGE_NAME}:${TAG}"
echo ""
echo "Run:"
echo "  docker run -d \"
echo "    -p 80:80 -p 443:443 -p 5678:5678 -p 8000:8000 \"
echo "    -v /var/run/docker.sock:/var/run/docker.sock \"
echo "    -e OPENAI_API_KEY=your-key \"
echo "    -e GITHUB_TOKEN=your-token \"
echo "    --name ases \"
echo "    ${IMAGE_NAME}:${TAG}"
