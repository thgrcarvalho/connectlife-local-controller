# ConnectLife Local Controller

Local/hybrid control for a compatible Hisense air conditioner that uses the
ConnectLife cloud. The project combines normal ConnectLife API control with a
local replay fallback for recorded commands.

**No credentials, device identifiers, keys, signed captures, or personal logs
are included in this repository.** Each user must capture commands from their
own AC. The capture procedure is reusable; the captured bytes are not.

## Features

- Online control through the ConnectLife API.
- Health checks for cloud reachability, appliance connectivity, and the relay's
  local AC connection, with no replay flapping while the AC is absent.
- Automatic fallback to local replay after three actionable online health failures.
- Automatic return to online mode after two successful cloud checks, with time
  for the appliance to reconnect before its connectivity is enforced again.
- Relay, replay, and DNS process supervision with bounded exponential restart backoff.
- Serialized health transitions and client commands on one event loop, with
  cancellable local I/O and bounded queued/action execution time.
- Thirty-second healthy cloud polling and five-second failure/recovery polling.
- Power, temperature, mode, fan-speed, and Sleep commands when recorded.
- Online self-diagnosis and offline diagnosis from live fault flags.
- Status updates when the physical remote changes the AC.
- Windows Credential Manager storage for ConnectLife credentials.
- Localhost-only command sockets and background startup without a console.
- Capture inspection without printing captured payloads.

This is not a general implementation of the ConnectLife encryption protocol.
Offline replay supports only commands recorded from the same AC. It does not
implement provisioning, factory reset, OTA updates, or key recovery.

## Architecture

```text
Desktop CLI / another localhost client
                |
                v
      controller.py (one instance)
          |             |
   healthy cloud    cloud unavailable
          |             |
 ConnectLife API    replay_server.py
          |             |
     Hisense cloud      local AC

relay.py: real-cloud relay and capture path
windows_dns.py: narrow hostname override forwarded to the PC
```

Online mode allows the ConnectLife app and PC control to coexist. During
standalone replay, ConnectLife considers the AC offline. Offline control
requires the PC and AC to be on the same LAN.

## Requirements

- A compatible ConnectLife AC whose firmware accepts the local test
  certificate. This was verified on one Hisense window AC; other models and
  firmware versions may not work.
- A Windows PC with Python 3.12. WSL is optional for development tools, but the
  reliable runtime layout uses only native Windows files and processes.
- A router that can override one DNS hostname without making the PC the DNS
  server for the whole network.
- Administrator privileges for low-numbered ports and firewall rules.
- The Python packages in `requirements.txt`.

The relay/replay services use TCP 443 and the DNS proxy uses UDP 53. Keep them
on a trusted LAN only.

## Installation

For a dependable startup, deploy the controller on a local Windows filesystem,
not through a `\\wsl.localhost` path. Install its dependencies from the repository
root:

```powershell
python.exe -m pip install --target fakecloud\.windows-deps -r requirements.txt
```

WSL is useful for the optional capture-inspection tools:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Generate a new local certificate. Do not reuse certificates from another user:

```sh
bash fakecloud/gen_cert.sh
```

`server.crt` and `server.key` are generated locally and ignored by Git. Bash and
OpenSSL are needed only to generate the certificate, not by the running service.

Copy and edit the controller configuration:

```powershell
Copy-Item config.example.json fakecloud\config.json
```

Configuration fields:

- `bind_ip`: this PC's LAN IP.
- `ac_ip`: the target AC's LAN IP. The relay uses it to isolate local health and
  command state from any other ConnectLife devices on the network. A DHCP
  reservation for the AC is recommended.
- `upstream_dns`: the normal LAN resolver used by the DNS proxy, usually the
  ISP modem/router.
- `cloud_upstream_dns`: a non-hijacked resolver, such as a public DNS service,
  used to discover the real device-link address whenever the relay reconnects.
  Do not use the router that contains the device-link hostname override.
- `cloud_upstream_ip`: optional fallback if dynamic resolution fails. Resolve it
  outside the hijack before adding it. `null` disables the fallback.
- `hijack_domain`: normally `ac-eu-link.hijuconn.com`.
- `offline_capture`: capture basename, without `.c2s.bin` or `.s2c.bin`.
- `command_map`: JSON file mapping action names to captured command indices.
- `reconnect_grace_seconds`: time allowed for the AC to reconnect after entering
  online mode before appliance disconnects count as failed health checks.
- `local_control_retry_seconds`: deadline for replay's localhost control service
  to become ready when a client request arrives immediately after failover.
- `health_interval_seconds`: cloud polling interval while healthy; defaults to 30.
- `health_retry_seconds`: polling interval during failures and offline recovery;
  defaults to 5. Child supervision continues every five seconds independently.
- `rehearsal_interval_seconds`: optional offline rehearsal interval; `0` disables
  rehearsal. See the rehearsal section before enabling it.
- `rehearsal_timeout_seconds` and `rehearsal_command`: reconnect deadline and the
  recorded command used by an enabled rehearsal.

Relative capture paths are resolved from `fakecloud/`. `config.json` is ignored
by Git. `AC_LOCAL_CONFIG` can point to a configuration in another location.

## Router DNS configuration

Override **only** `ac-eu-link.hijuconn.com`, pointing it to the PC. Leave the
router's WAN DNS automatic. Do not point the whole network's DNS at the PC;
other devices would lose DNS whenever the PC is off.

For routers using OpenWrt-style UCI, the persistent configuration is:

```uci
config domain
    option name 'ac-eu-link.hijuconn.com'
    option ip '192.168.31.100'
```

Replace the example IP with the PC's actual LAN IP. Apply the configuration and
restart the router's DNS service. A `/etc/hosts` or `/etc/custom_hosts` entry
may be temporary on some stock firmware and is not a substitute for verifying
persistence. Reboot the router and confirm both of these results:

```sh
nslookup ac-eu-link.hijuconn.com 192.168.31.1  # returns the PC IP
nslookup example.com 192.168.31.1              # returns normal public addresses
```

Replace `192.168.31.1` with the router's address. On other routers, use the
corresponding static-DNS/dnsmasq hostname override.

The relay resolves the real cloud hostname through `cloud_upstream_dns` on each
new upstream connection. This resolver must be outside the hostname override.
An optional verified `cloud_upstream_ip` is used only if that lookup fails.

## Capture signed commands from your own AC

Captures can contain session tokens, device identifiers, status data, and
signed commands. Keep them private. The `fakecloud/captures/` directory is
ignored by Git.

1. Generate the certificate and configure the single-host DNS override.
2. Start the real-cloud relay with the PC LAN IP and a non-hijacked DNS resolver:

   ```powershell
   python.exe fakecloud\relay.py --bind 192.168.31.100 --upstream-dns 1.1.1.1
   ```

   Replace the example bind address. Raw payloads are not logged by default.
   `--log-payloads` exists for deliberate local research and may expose secrets.

3. Reconnect or power-cycle only the AC if necessary to make it establish a new
   TLS session through the relay.
4. In one uninterrupted ConnectLife session, perform every command you want to
   replay. Include the complete handshake, power on/off, every temperature,
   modes, fan speeds, and Sleep on/off as applicable.
5. Stop the relay after the commands have been acknowledged. It writes:

   ```text
   fakecloud/captures/<timestamp>-<connection>.c2s.bin
   fakecloud/captures/<timestamp>-<connection>.s2c.bin
   ```

6. List the captured cloud-to-AC command indices without printing payloads:

   ```sh
   .venv/bin/python fakecloud/capture_tools.py list \
     fakecloud/captures/<capture>.s2c.bin
   ```

   Indices are zero-based among `id=201` frames, not among all frames. Compare
   this list with the order in which you performed commands in the app.

7. Create `fakecloud/captures/commands.json` using
   `examples/commands.json` as a starting point. Supported controller actions
   are `on`, `off`, `temp16` through `temp30`, `fan`, `cool`, `dry`, `eco`,
   `fanlow`, `fanmed`, `fanhigh`, `sleep`, and `sleepoff`.

8. Validate the map:

   ```sh
   .venv/bin/python fakecloud/capture_tools.py validate \
     fakecloud/captures/<capture>.s2c.bin fakecloud/captures/commands.json
   ```

9. Set `offline_capture` in `fakecloud/config.json` to
   `captures/<capture>` and `command_map` to `captures/commands.json`.

**Use one complete capture session. Do not mix command frames from different
sessions.** A command map alone is insufficient; the matching `.s2c.bin` file
and the complete recorded handshake responses are required.

## Standalone replay

Replay does not contact the cloud and does not require the device key:

```powershell
python.exe fakecloud\replay_server.py --bind 192.168.31.100 `
  --capture fakecloud\captures\<capture> `
  --command-map fakecloud\captures\commands.json
```

Use another terminal for status and commands:

```powershell
python.exe fakecloud\replay_cli.py status
python.exe fakecloud\replay_cli.py on
python.exe fakecloud\replay_cli.py temp26
python.exe fakecloud\replay_cli.py sleepoff
```

ConnectLife is offline while replay owns the AC connection. Stop replay and
start the relay (or the hybrid controller) to return to real-cloud operation.

## Hybrid controller

Store your ConnectLife credentials once in Windows Credential Manager:

```powershell
cd fakecloud
python.exe controller_cli.py setup
python.exe controller_cli.py run
```

The credential target is `ConnectLife/local-controller`. Credentials are not
written to `config.json` or the repository. `reset-credentials` deletes the
stored entry.

From another terminal in `fakecloud/`:

```powershell
python.exe controller_cli.py status
python.exe controller_cli.py diagnose
python.exe controller_cli.py on
python.exe controller_cli.py off
python.exe controller_cli.py temp26
python.exe controller_cli.py eco
python.exe controller_cli.py fanhigh
python.exe controller_cli.py sleep
python.exe controller_cli.py sleepoff
```

The controller validates mode constraints: temperature requires power on and a
temperature-capable mode, Fan mode has no temperature or Sleep control, and Dry
mode fixes fan speed to High. Client actions, health checks, and mode changes
are serialized so they cannot modify shared API and connection state concurrently.
A server-side 30-second deadline covers both lock waiting and execution. Requests
whose clients disconnect while queued are discarded before execution.

Cloud API failures can trigger offline replay whether or not the AC is present.
If the API is reachable but reports the appliance offline, the controller checks
the relay's localhost status for `ac_ip`; other devices using the relay have
isolated state and cannot affect this decision. A live relay-to-AC connection
makes replay useful; no local connection means the AC is absent, so the
controller waits instead of flapping between modes. Offline recovery initially requires only cloud
reachability; after switching online, `reconnect_grace_seconds` allows the AC to
reconnect. The controller restarts missing or exited relay, replay, and DNS
children with bounded exponential backoff after repeated failures.

For invisible startup at Windows login, create a Startup shortcut that runs
`pythonw.exe controller_cli.py run` using an absolute native Windows script path
and a native Windows `fakecloud/` working directory. Do not use a WSL UNC path
for this runtime shortcut. Keep exactly one controller instance running.

## Optional offline rehearsal

Replay compatibility can change if the cloud rotates session material or a
firmware update changes the protocol. An optional rehearsal can periodically
verify that the AC acknowledges one selected recorded command before a real
outage. It does not prove that every command or every future capture will work.

Rehearsal is disabled by default. To enable it, set
`rehearsal_interval_seconds` to a positive interval and choose a
`rehearsal_command` present in the command map. Each rehearsal:

1. Stops the real-cloud relay and starts offline replay.
2. Waits up to `rehearsal_timeout_seconds` for the AC to reconnect.
3. Sends the selected command and requires its acknowledgment.
4. Restores online relay mode, including after a failed rehearsal.

This temporarily disconnects ConnectLife and changes the selected AC setting;
the original setting is not automatically restored. Choose a command whose
effect is acceptable and do not enable frequent rehearsals.

## Diagnosis

Online diagnosis uses ConnectLife's self-check API and reports the entries in
`selfCheckFailedList`. Offline diagnosis evaluates live fault flags for:

- Indoor room and coil temperature sensors.
- Indoor/outdoor communication.
- Indoor control-panel EEPROM.
- Overcooling and overheating protection.

`self_check_inspect.py` is an optional standalone metadata inspector. Its output
file contains identifiers and is ignored by Git.

## Troubleshooting

- **Missing configuration:** copy `config.example.json` to
  `fakecloud/config.json` and replace every placeholder.
- **AC does not reconnect:** confirm the router returns the PC IP only for the
  device-link hostname and that TCP 443/UDP 53 are allowed on the LAN.
- **Relay loops back to itself:** configure `cloud_upstream_dns` as a resolver
  outside the hostname override. Check any optional `cloud_upstream_ip` fallback
  in the same way.
- **Offline command is unavailable:** record that action in the same complete
  capture and update the command map.
- **ConnectLife shows the AC offline:** this is expected during standalone
  replay; return to online mode for simultaneous app/cloud access.
- **DNS fails when the PC is off:** the router is still using the PC as its
  global DNS server. Restore automatic WAN DNS and keep only the hostname
  override.
- **After a router reboot:** verify that the override persists. A temporary
  hosts file may be regenerated or deleted by stock firmware.

## Security and limitations

Use this only on devices and networks you own or are authorized to administer.
The tested firmware accepts a self-signed certificate for the local relay.
Keep capture files, payload logs, credentials, and certificates private, and do
not expose the relay or control sockets to untrusted networks.

No firmware is flashed by this project. No OTA, provisioning, reset, or key
operations are supported. A future firmware update may change compatibility.

## License

MIT. See [LICENSE](LICENSE).
