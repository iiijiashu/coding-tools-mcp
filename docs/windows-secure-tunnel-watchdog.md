# Windows Secure MCP Tunnel watchdog

`tunnel-client` can intentionally shut down after its local stdio MCP command
exits. On Windows that shutdown may still return exit code `0`, so Task
Scheduler's **Restart on failure** setting alone cannot guarantee recovery.

`scripts/secure_tunnel_watchdog.py` is a short-lived companion task. The
primary scheduled task continues to launch and own `tunnel-client.exe`; the
watchdog only observes and repairs that exact task:

- a `Ready` primary task is started regardless of its previous exit code;
- a running task is checked with `tunnel-client health`, including PID matching
  and a successful control-plane poll;
- the local MCP backend must complete `initialize`, `tools/list`, and
  `server_info`, match the fixed workspace, and expose the required catalog
  before the tunnel may run;
- one failed health probe is recorded without disruption;
- two consecutive failed probes restart only the named primary task;
- an unavailable or identity-mismatched local backend stops the exact tunnel
  task and keeps it unpublished until the backend is ready;
- failed restarts of a still-running but locally unhealthy task use persistent
  1, 2, 5, 10, then 30 minute backoff, including when the watchdog itself is
  interrupted mid-attempt; a `Ready` task bypasses that cooldown because the
  one-minute task cadence already throttles clean-exit recovery; the backoff
  resets only after five continuous healthy minutes;
- a disabled primary task remains disabled.

Use a one-minute repeating scheduled task with `MultipleInstances=IgnoreNew`.
For an interactive user task, launch the short-lived watchdog with the virtual
environment's `pythonw.exe`, not `python.exe`, so the one-minute probe never
creates a console window. Keep failures in the JSONL event log; the watchdog
does not require an attached stdout or stderr stream. The same rule applies to
the companion local HTTP watchdog and its long-running HTTP service wrapper.
Disable the primary task's native `RestartOnFailure`; otherwise non-zero exits
bypass the watchdog's persisted backoff and can create a competing restart
loop.
Set its execution limit to at least three minutes, and scope every operation by
both the exact task name and task path. On a laptop, give the primary task and
watchdog the same battery policy; this deployment allows both to start and keep
running on battery.
Give the primary tunnel task an ephemeral loopback health listener and a
dedicated `--health.url-file`. Keep its profile, PID, health URL, state, and log
paths separate from every other tunnel.

The watchdog emits a compact JSONL event log and an atomic state file. It never
records the profile contents, runtime API key, raw health response, or MCP tool
payloads. The event log rotates at 5 MiB and keeps three backups by default.

To stop the tunnel intentionally, disable the watchdog before stopping the
primary task, or disable the primary task itself. Merely stopping a task is
treated as a recoverable outage.

## Verification

1. Run `tunnel-client doctor --profile-file <profile> --explain`.
2. Confirm the primary task is `Running`.
3. Run `tunnel-client health --url-file <health-url-file> --pid-file <pid-file>
   --require-control-plane-poll --json`.
4. Stop the local MCP task, invoke the watchdog once, and verify the tunnel is
   stopped rather than publishing an unhealthy backend. Restore local MCP and
   verify its watchdog completes MCP identity and catalog checks first.
5. Stop only the tunnel task, invoke the watchdog once, and verify that the
   primary task returns to `Running` and health becomes ready.
6. Verify another tunnel's PID, profile, PID file, and health endpoint did not
   change.
7. Finish with a real MCP `server_info`, read, patch, and asynchronous command
   call through the OpenAI product that consumes the tunnel.

## Rollback

First verify the backup SHA-256 recorded during installation. Then run the
following with the exact task and backup paths used by that deployment:

```powershell
Disable-ScheduledTask -TaskPath '\' -TaskName '<watchdog task>'
Stop-ScheduledTask -TaskPath '\' -TaskName '<watchdog task>'
Register-ScheduledTask -TaskPath '\' -TaskName '<primary task>' `
  -Xml (Get-Content -LiteralPath '<primary task XML backup>' -Raw) -Force
Unregister-ScheduledTask -TaskPath '\' -TaskName '<watchdog task>' -Confirm:$false
```

The MCP profile and runtime key do not need to change. State and rotated event
logs can be retained for diagnosis or removed later as a separate exact-path
cleanup.
