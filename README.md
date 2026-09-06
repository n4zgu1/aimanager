# AI Model Manager

A simple web UI and API for managing local AI models with persistent VRAM loading.

## Features

- List all available local AI models
- Load selected model into VRAM (remains loaded until manually switched)
- Web UI accessible at `/aimanager` path (to work with existing nginx setup)
- `aimanager` CLI for terminal access (installed to `/usr/local/bin/aimanager`)

## Prerequisites

1. Ollama installed and running
2. Python 3.8+ 
3. Access to local AI models in Ollama's model store

## Installation

```bash
# Make sure Ollama is running on port 11434
# (It's usually auto-started when installing Ollama)
```

## Usage

### Start the server:

```bash
python app.py
```

The API will be available on port `22345`, and the web UI will be accessible at `/aimanager` path.

### Web UI

With nginx configured, access the web UI via:
- `http://<your-server>/aimanager` (redirects to the web interface)
- `http://<your-server>:22345/aimanager` (direct access)

### Command Line

The `aimanager` CLI is installed as `/usr/local/bin/aimanager` and can be run from anywhere:

```bash
aimanager list               # list all available models
aimanager load qwen3.5:4b    # load a model into VRAM
aimanager unload qwen3.5:4b  # unload a model from VRAM
aimanager current            # show the currently loaded model
aimanager gpu                # show live amd-smi GPU snapshot
aimanager help               # show all commands
```

The service URL can be overridden with the `AIMANAGER_URL` env var (default `http://localhost:22345/aimanager`).

## API Endpoints

- `GET /aimanager/models` - List all available models (full names incl. tags)
- `POST /aimanager/load` - Load a model into VRAM and keep it loaded (`keep_alive: -1`)
- `POST /aimanager/unload` - Unload a model from VRAM (`keep_alive: 0`)
- `GET /aimanager/current` - Show the currently loaded model
- `GET /aimanager/terminal` - Stream an `amd-smi` GPU snapshot (plain text)
- `GET /aimanager/fan` - Get GPU fan status (RPM/PWM)
- `POST /aimanager/fan` - Set GPU fan speed (`percent` 0-100) or `reset`
- `GET /aimanager/serverfans` - List remote server fans/temps from `fanapi` (`http://192.168.1.84:22395`)
- `POST /aimanager/serverfans` - Control a remote server fan (`{"index": 1, "percent": 50}` or `{"index": 1, "mode": "manual|auto|off"}`)
- `GET /aimanager/gpu` - Structured GPU metrics/processes parsed from `amd-smi --json` (feeds the web UI GPU table)

All endpoints accept/return JSON. Both work directly on port 22345 and via nginx on port 80.

## How It Works

The application interfaces with Ollama's REST API. When you load a model:
1. It sends a `keep_alive: -1` request to Ollama's `/api/generate` endpoint
2. This loads the model into VRAM and keeps it loaded until explicitly unloaded
3. The model stays in VRAM until the user switches to another model or calls `/unload`
4. `/unload` sends `keep_alive: 0`, which frees the model from VRAM

## Integration with nginx

The live config lives at `/etc/nginx/sites-available/aimanager` (symlinked into `sites-enabled`). It adds the `/aimanager` location to the `default_server` block so the path works for any Host/IP (e.g. `http://192.168.1.237/aimanager`).

```nginx
# Add to your server block — IMPORTANT: no trailing slash on proxy_pass,
# and do NOT add a "location = /aimanager" redirect block (causes a redirect loop).
location /aimanager {
    proxy_pass http://localhost:22345;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# Optional: redirect root to the AIMANAGER UI
location = / {
    return 302 /aimanager/;
}
```

## Service Management

The application runs as a systemd service named `aimanager`:

```bash
# Start the service
sudo systemctl start aimanager

# Stop the service
sudo systemctl stop aimanager

# Restart the service
sudo systemctl restart aimanager

# Check service status
sudo systemctl status aimanager

# Enable auto-start on boot
sudo systemctl enable aimanager
```