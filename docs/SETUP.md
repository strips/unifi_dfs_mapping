# Setup: UniFi UDR + host

## 1. Configure the UDR to send syslog

UniFi Network → Settings → System → Application Logging:
* **Remote Syslog Server** — enable
* **Host** — IP of the machine running this container
* **Port** — `514`
* **Protocol** — UDP

## 2. Create a *local-only* admin user

UniFi Network → Settings → Admins → **Add new admin** →
*"Restrict to local access only"*. Give it `Super Admin` (the REST API
endpoints we use require write access to device records).

Put the credentials in `secrets/secrets.env`:

```
UNIFI_USERNAME=fjord_radar
UNIFI_PASSWORD=<paste here>
```

`chmod 600 secrets/secrets.env`. The file is gitignored.

> Why not the new "API key" feature? UniFi's API keys (created under
> Settings → Control Plane → Integrations) only work against the
> *cloud* Site Manager API, not the local controller. Local automation
> still requires user/password — that's what `pyunifi`,
> `aiounifi` and Home Assistant all use.

## 3. Find the AP name and radio identifier

In the UniFi UI, the AP appears under Devices with a display name like
`AC-HD` (or whatever you renamed it to). Put that into
`config/config.yaml` as `target.ap_name`. The radio identifier for
5 GHz is `na` (the legacy 802.11n/ac string), `ng` for 2.4 GHz, `6e`
for 6 GHz.

## 4. PUID / PGID

The container runs as the UID/GID set in `.env`. They must match the
owner of `./data/`:

```bash
echo "PUID=$(id -u)" >> .env
echo "PGID=$(id -g)" >> .env
```

This avoids the "unable to open database file" crash you'll see if the
container UID doesn't have write on the bind-mounted directory.

## 5. Port 514 / sudo

Docker (running as root) handles privileged-port binding for you. If
your user is in the `docker` group, **no sudo is needed at runtime**:

```bash
groups | tr ' ' '\n' | grep -x docker || sudo usermod -aG docker "$USER"
# log out / back in, or:
newgrp docker
```

## 6. Verify packets are arriving

```bash
docker compose logs -f fjord-radar
sudo tcpdump -i any -n udp port 514
```

You should see `event kind=...` lines appear within seconds of an AP
performing a CAC, channel switch, or radar detection.

## 7. Web access

By default the stats page is exposed on `0.0.0.0:8080` (your LAN). To
restrict to localhost only, set in `.env`:

```
FJORD_WEB_PORT=8080
```

and in `config/config.yaml`:

```yaml
web:
  bind_host: "127.0.0.1"
```

Also change the compose port mapping line to `"127.0.0.1:8080:8080/tcp"`
if you want a hard guarantee Docker won't expose it.
