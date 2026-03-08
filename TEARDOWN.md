# Tearing Down sysdashboard (Migration to inframatik)

Instructions for removing the old `sysdashboard` installation from a machine. Run these as the same user that runs sysdashboard (typically `aiml`).

## Quick Version (copy-paste)

```bash
# 1. Stop all managed services
for svc in $(systemctl --user list-units --type=service --no-legend | grep 'sysdash-' | awk '{print $1}'); do
    systemctl --user disable --now "$svc"
done

# 2. Stop sysdashboard itself
systemctl --user disable --now sysdashboard.service

# 3. Remove all unit files
rm -f ~/.config/systemd/user/sysdash*.service

# 4. Reload systemd
systemctl --user daemon-reload
systemctl --user reset-failed

# 5. Remove config directory
rm -rf ~/.config/sysdashboard/

# 6. Clean .bashrc
sed -i '/sysdashboard/d' ~/.bashrc

# 7. Remove old sudoers (if CF was set up)
sudo rm -f /etc/sudoers.d/sysdash-cf

# 8. Verify clean state
systemctl --user list-units --type=service --no-legend | grep sysdash
ss -tlnp | grep 9000
# Both should return nothing
```

## What This Does

1. **Stops all managed services** (sysdash-*) — your apps stop running
2. **Stops sysdashboard** — the dashboard process stops
3. **Removes systemd unit files** — prevents services from auto-starting on reboot
4. **Reloads systemd** — clears any cached unit state
5. **Removes config** — deletes `~/.config/sysdashboard/` (node.json, services.json, ports.env, cf.env)
6. **Cleans .bashrc** — removes the `ports.env` sourcing line
7. **Removes sudoers rule** — if Cloudflare was set up, removes the passwordless sudo rule for `sysdash-cf-setup`

## What This Does NOT Do

- **Does not delete `~/sysdashboard/`** — the old code directory is left as a backup. Delete manually with `rm -rf ~/sysdashboard/` when ready.
- **Does not remove cloudflared** — if cloudflared is installed, it stays. inframatik will reuse it.
- **Does not affect other systemd services** — only `sysdash*` units are touched.

## After Teardown

The machine is ready for a fresh inframatik install. Either:

- **Manual setup**: Clone the repo, create venv, run uvicorn (see README.md)
- **Install script**: From the master, `curl -fsSL http://MASTER:9000/api/install.sh | bash`

## Notes for Worker Nodes

Worker nodes may still be trying to heartbeat to the old master. After teardown:
- The heartbeat loop is stopped (sysdashboard service disabled)
- The old master won't receive heartbeats (it's also torn down)
- When you set up inframatik, workers will need to re-enroll using the new enrollment token system

## Cloudflare Tunnels

If the old sysdashboard had CF tunnels configured:
- The tunnel still exists in your CF account (not deleted by teardown)
- cloudflared may still be running as a user service: `systemctl --user status cloudflared`
- You can stop it: `systemctl --user disable --now cloudflared`
- Legacy installs may still have a system service: `sudo systemctl disable --now cloudflared`
- Or leave it running — inframatik will reconfigure it when you set up CF through the new wizard
