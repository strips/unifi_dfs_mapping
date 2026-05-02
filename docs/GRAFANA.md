# Grafana integration

Two paths, depending on whether you already run Grafana.

## A. You already run Grafana

This is the recommended path — running two Grafanas on one box is silly.

### A1. Make Grafana able to read our SQLite

1. Install the SQLite datasource plugin in your existing Grafana:

   ```bash
   grafana-cli plugins install frser-sqlite-datasource
   sudo systemctl restart grafana-server
   ```

   (or via env in your Grafana docker container:
   `GF_INSTALL_PLUGINS=frser-sqlite-datasource`)

2. Make sure the Grafana process can read `data/events.db`. The
   simplest pattern is to bind-mount this project's `data/` directory
   read-only into your Grafana container, e.g.:

   ```yaml
   # in your existing grafana docker-compose.yml
   volumes:
     - /path/to/unifi_dfs_mapping/data:/data/fjord:ro
   ```

3. In Grafana → Connections → Data sources → Add → SQLite:
   - Name: `Fjord-SQLite`
   - Path: `/data/fjord/events.db` (or whatever path you mounted)

### A2. Import the dashboard

`grafana/provisioning/dashboards/fjord-radar.json` is a portable
dashboard JSON. Import it via:

* **UI:** Dashboards → New → Import → paste the JSON.
* **Provisioning:** copy the file under your Grafana's
  `provisioning/dashboards/` directory and add a `dashboards.yml`
  provider entry pointing at it. The file under
  `grafana/provisioning/dashboards/dashboards.yml` is exactly such an
  entry; you can copy both into your Grafana provisioning tree.
* **HTTP API:**
  ```bash
  curl -sS -X POST -H "Content-Type: application/json" \
       -H "Authorization: Bearer $GRAFANA_API_TOKEN" \
       -d "$(jq '{dashboard: ., overwrite: true}' grafana/provisioning/dashboards/fjord-radar.json)" \
       https://grafana.example.com/api/dashboards/db
  ```

### A3. Don't start the bundled Grafana

Just `docker compose up -d fjord-radar` (without the `--profile viz`
flag). Our compose only starts Grafana when that profile is active.

## B. You don't have Grafana yet

Use the bundled one:

```bash
# secrets/secrets.env must contain GF_SECURITY_ADMIN_PASSWORD=...
docker compose --profile viz up -d --build
```

Grafana is bound to `127.0.0.1:3000` by default — see
`docker-compose.yml`. The SQLite datasource and dashboard are
auto-provisioned from `grafana/provisioning/`.

## Built-in vs Grafana

Grafana shines for time-series exploration over many months. For this
project's actual question — "which channel/width has the highest MTBD"
— the built-in stats page at `http://<host>:8080/` is **sharper**: a
single sorted table that updates every minute, no plugin to install,
no datasource to wire up. Use Grafana once you want to slice radar
events by hour-of-day, day-of-week, weather conditions, etc.
