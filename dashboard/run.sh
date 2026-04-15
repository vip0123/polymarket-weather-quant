#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec uv run --extra dashboard streamlit run app.py --server.port=8501 --server.headless=true
