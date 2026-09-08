# Baidu Netdisk low-traffic MCP POC

This is an isolated proof of concept for uploading generated files to Baidu Netdisk while keeping proxy and VPS traffic near zero.

## What this POC proves

- **Local data plane:** file bytes go from the machine running this MCP process directly to Baidu's upload endpoint. There is no VPS relay in this implementation.
- **Bounded memory:** the default block is 4 MiB; hashing and uploading operate block-by-block instead of loading the full file.
- **Missing-part upload:** after `precreate`, only indexes returned in `block_list` are uploaded. Server-known blocks are not resent.
- **Rapid upload:** `return_type=2` stops after precreate, so file bytes are not uploaded again.
- **Secret separation:** `BAIDU_NETDISK_ACCESS_TOKEN` is read from the process environment and is not a tool argument. Error text is token-redacted.
- **MCP SDK v2:** the server uses `mcp.server.MCPServer`, not the removed v1 `FastMCP` import.

The upload flow follows Baidu's current Open Platform sequence: `precreate -> upload missing slices -> create`.

## Resource model

| Component | File bytes | Expected cost |
|---|---:|---|
| ChatGPT/MCP control messages | No | KB-scale |
| VPS | No | ~0 file-transfer traffic |
| Local host -> Baidu | Yes | roughly the missing file data only |
| Clash/proxy | Should be bypassed with DIRECT rules | ~0 proxy quota |

This POC intentionally does **not** implement a remote URL relay. A relay would make the VPS forward the file and consume roughly the file size in egress.

## Setup

Use Python 3.11+ in this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

Set the token in the local process. Do not paste the token into a chat:

```powershell
$env:BAIDU_NETDISK_ACCESS_TOKEN = "<set-locally>"
```

Run the stdio MCP server:

```powershell
.\.venv\Scripts\baidu-netdisk-low-traffic.exe
```

Example MCP client configuration:

```json
{
  "mcpServers": {
    "baidu-netdisk-low-traffic": {
      "command": "D:\\coding-tools-mcp\\integrations\\baidu-netdisk\\.venv\\Scripts\\baidu-netdisk-low-traffic.exe",
      "env": {
        "BAIDU_NETDISK_ACCESS_TOKEN": "${BAIDU_NETDISK_ACCESS_TOKEN}"
      }
    }
  }
}
```

The exposed tool is intentionally narrow:

```text
baidu_upload_file(local_file_path, remote_dir="/来自：ChatGPT")
```

It cannot execute shell commands, fetch arbitrary URLs, delete Netdisk files, or read a token supplied by the model.

## Clash bypass

The data plane should be routed DIRECT. Start narrowly with the observed Baidu Open Platform hosts:

```yaml
rules:
  - DOMAIN,pan.baidu.com,DIRECT
  - DOMAIN,d.pcs.baidu.com,DIRECT
```

Do not add a broad `DOMAIN-SUFFIX,baidu.com,DIRECT` rule unless every Baidu service should bypass the proxy.

## Checkpoints

### P0 — implemented here

- unit-test precreate/missing-slice/create behavior
- prove rapid-upload path sends no file parts
- prove only bounded chunks are passed to the HTTP layer
- prove token is not returned in transport errors

### P1 — requires local credentials, not chat credentials

Create a 10 MiB random test file locally and call the tool once. Record:

- returned `fs_id` / remote path
- local NIC bytes sent to Baidu
- VPS bytes: expected ~0
- proxy quota change: expected ~0 with DIRECT rules

### P2 — ChatGPT artifact bridge

This remains a separate compatibility checkpoint. A ChatGPT-generated attachment is not assumed to be a local filesystem path visible to an stdio MCP process. Before any relay is built, verify whether the current ChatGPT App/MCP surface can expose that generated file to the local uploader without copying its bytes through a VPS. If not, choose an explicit fallback and keep remote relay opt-in and size-capped.

## Tests

```powershell
pytest -q
```

The tests use fake HTTP responses and do not contact Baidu or require a real token.
