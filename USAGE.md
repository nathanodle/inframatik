# Service Management

## From the Web UI

1. Open the inframatik dashboard and log in
2. Click **+ New Service**
3. Fill in the form:
   - **Name**: lowercase, hyphens/underscores OK (e.g. `my-app`)
   - **Command**: your start command — no port needed (e.g. `uvicorn main:app --host 127.0.0.1`)
   - **Working Directory**: absolute path (e.g. `/home/user/my-app`)
   - **CF Hostname** (optional): if you want it publicly accessible (e.g. `myapp.example.com`)
   - **LAN accessible** (optional): bind to `0.0.0.0` instead of `127.0.0.1`
4. Click **Create**
5. Click **Start** on the service card

## From the API

All API requests require authentication. Use either:
- **Session token**: `Authorization: Bearer <token>` (from `/api/auth/login`)
- **Service token**: `Authorization: Bearer svc_...` (from `inframatik init` or dashboard)

### Register a service

```bash
curl -X POST http://localhost:9000/api/services \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-app",
    "command": "uvicorn main:app --host 127.0.0.1",
    "working_dir": "/home/user/my-app",
    "hostname": "myapp.example.com"
  }'
```

This will:
- Assign the next available port (8000–8999)
- Create a systemd user service with `PORT` and `HOST` env vars
- If hostname provided and Cloudflare is configured: add tunnel route + DNS record + Access app
- Update `~/.config/inframatik/ports.env` with `INFRA_MY_APP_PORT=<port>`

The `hostname` field is optional — omit it for services that should only be reachable locally.

### Start it

```bash
curl -X POST http://localhost:9000/api/services/my-app/start \
  -H "Authorization: Bearer $TOKEN"
```

### Other actions

```bash
# Stop
curl -X POST http://localhost:9000/api/services/my-app/stop \
  -H "Authorization: Bearer $TOKEN"

# Restart
curl -X POST http://localhost:9000/api/services/my-app/restart \
  -H "Authorization: Bearer $TOKEN"

# View logs
curl http://localhost:9000/api/services/my-app/logs \
  -H "Authorization: Bearer $TOKEN"

# Remove entirely (stops service, removes unit, CF route, DNS, Access app)
curl -X DELETE http://localhost:9000/api/services/my-app \
  -H "Authorization: Bearer $TOKEN"

# List all services
curl http://localhost:9000/api/services \
  -H "Authorization: Bearer $TOKEN"
```

## From an AI Agent

If you've run `inframatik init` in your repo, the `.inframatik` file contains everything the agent needs: endpoint, scoped service token, and API instructions. The agent reads this file and uses `curl` or the MCP tools to manage the service.

See the [AI Agent Integration](README.md#ai-agent-integration) section in the README for setup details.

## How Ports Work

Ports are assigned automatically by inframatik. You never hardcode them.

**Your app reads the port from the `PORT` environment variable**, which is set by the systemd service unit. Most frameworks support this natively:

- **uvicorn**: `uvicorn main:app --host 127.0.0.1 --port $PORT`
- **vite**: reads `PORT` automatically
- **Node/Express**: `process.env.PORT`
- **Flask**: `flask run --port $PORT`
- **Streamlit**: `streamlit run app.py --server.port $PORT`
- **Any tool**: use `$PORT` in the command — env vars are expanded by the service

If your app reads `PORT` from env in its code, you don't need it in the command at all:

```python
import os, uvicorn
from fastapi import FastAPI
app = FastAPI()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]))
```

Command: just `python main.py`.

**In your shell**, every service port is available as `$INFRA_<NAME>_PORT`:

```bash
echo $INFRA_MY_APP_PORT     # 8001
curl localhost:$INFRA_MY_APP_PORT/api/health
```

These are sourced from `~/.config/inframatik/ports.env` via `.bashrc` (added automatically by the installer). Open a new terminal to pick up changes after registering a service.

## Important Notes

- **Port range**: 8000–8999, auto-assigned in order
- **Services run as your user** via systemd user services — they survive logout and start on boot
- **Cloudflare integration** is optional and configured through the Settings wizard — no manual config files needed
- **DNS propagation** may take a minute or two after registering a service with a hostname
- **Service tokens** (from `inframatik init`) are scoped to one service and cannot manage other services or access system configuration
