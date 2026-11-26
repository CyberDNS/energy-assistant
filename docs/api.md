# API and MCP

Through ingress the API needs no login. To read it from elsewhere — scripts,
or an AI assistant debugging the live installation over MCP — give it a token
and publish the port:

1. Set a token in `config.yaml` (generate one with `openssl rand -hex 32`):

   ```yaml
   server:
     api_token: !secret nornwatt_api_token
   ```

   or set `NORNWATT_API_TOKEN` in the environment.
2. In the add-on's **Network** settings, map container port `8088/tcp` to a
   host port (it is unmapped by default), then restart the add-on.

With a token set, every request that does not come through ingress (or from
localhost) must send `Authorization: Bearer <token>`, and only gets read
access: `GET` on the REST API and the MCP endpoint. Writes are refused (403);
the web UI behind ingress keeps full control. `/health` stays open.

The MCP endpoint is `http://<host>:<port>/mcp` (streamable HTTP, stateless).
Its tools are read-only wrappers around the REST API: `get_status`, `get_version`,
`get_plan` (full per-slot flows), `get_forecast`, `get_savings`,
`get_savings_slots` (real vs. simulated uncontrolled house per accounting
slot, with all cost components), `get_history`, `get_execution`,
`list_plan_snapshots` / `get_plan_snapshot`, `get_ledger`, `get_config`,
`get_vehicles`, `get_vehicle_plan`, `get_chargers`, `get_controllable`,
`get_learned_forecasts`, `get_plan_inputs`.
For Claude Code:

```bash
claude mcp add --transport http nornwatt http://<host>:<port>/mcp \
  --header "Authorization: Bearer <token>"
```
