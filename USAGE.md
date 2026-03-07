# Service Management

## From the Web UI

1. Open the inframatik dashboard
2. Click **+ New Service**
3. Fill in the form:
   - **Name**: lowercase, hyphens/underscores OK (e.g. `my-app`)
   - **Command**: your start command — no port needed (e.g. `uvicorn main:app --host 127.0.0.1`)
   - **Working Directory**: absolute path (e.g. `/home/user/my-app`)
   - **CF Hostname** (optional): if you want it publicly accessible (e.g. `myapp.example.com`)
4. Click **Create**
5. Click **Start** on the service card when you're ready to launch

## From the API

### Register your service

```bash
curl -X POST http://localhost:9000/api/services \
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
- Create a systemd user service with `PORT` env var set
- Add the CF tunnel route + DNS record (if hostname provided)
- Create a CF Access Application with the "your access policy"
- Update `~/.config/inframatik/ports.env` with `INFRA_MY_APP_PORT=<port>`

The `hostname` field is optional — omit it for services that should only be reachable on localhost.

### Start it

```bash
curl -X POST http://localhost:9000/api/services/my-app/start
```

### Other actions

```bash
# Stop
curl -X POST http://localhost:9000/api/services/my-app/stop

# Restart
curl -X POST http://localhost:9000/api/services/my-app/restart

# View logs
curl http://localhost:9000/api/services/my-app/logs

# Remove entirely (stops service, removes unit, CF route, DNS, Access app)
curl -X DELETE http://localhost:9000/api/services/my-app

# List all services
curl http://localhost:9000/api/services

# Check next available port
curl http://localhost:9000/api/ports/next
```

## How Ports Work

Ports are assigned automatically by inframatik. You never hardcode them.

**Your app reads the port from the `PORT` environment variable**, which is set by the systemd service unit. Most frameworks support this natively:

- **uvicorn**: `uvicorn main:app --host 127.0.0.1 --port $PORT`
- **vite**: reads `PORT` automatically
- **Node/Express**: `process.env.PORT`
- **Flask**: `flask run --port $PORT`
- **Any tool**: use `$PORT` in the command — env vars are expanded in the service

If your app reads `PORT` from env in its code, you don't need it in the command at all:

```python
# main.py
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

These are sourced from `~/.config/inframatik/ports.env` via `.bashrc`. Open a new terminal to pick up changes after registering a service.

## Important Notes

- **Port range**: 8000–8999. Ports are auto-assigned in order.
- **Services run as your user** (your user) via systemd user services. They survive logout and start on boot.
- **CF integration is fully automated**. When you provide a hostname, the dashboard creates the tunnel route, DNS record, and CF Access Application with the default policy. Deletion cleans up all three.
- **DNS propagation** may take a minute or two after registering a new service with a hostname.
