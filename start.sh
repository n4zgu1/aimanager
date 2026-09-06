#!/bin/bash

echo "Starting AI Model Manager..."
echo "API will be available on port 22345"
echo "Web UI will be available at http://localhost:22344/ui"

# Check if python dependencies are installed
if ! pip show fastapi uvicorn httpx > /dev/null 2>&1; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

python app.py