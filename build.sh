#!/bin/bash
# Pxxl / Render build script
set -e

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo "=== Installing Playwright browsers ==="
python3 -m playwright install chromium
python3 -m playwright install-deps chromium || true

echo "=== Build complete ==="
