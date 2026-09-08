from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .client import BaiduNetdiskClient, BaiduNetdiskError, DEFAULT_REMOTE_DIR

mcp = MCPServer("baidu-netdisk-low-traffic")


@mcp.tool()
def baidu_upload_file(
    local_file_path: str,
    remote_dir: str = DEFAULT_REMOTE_DIR,
) -> dict[str, Any]:
    """Upload one local file directly from this host to Baidu Netdisk.

    The file bytes do not pass through a VPS relay. The access token is read from
    BAIDU_NETDISK_ACCESS_TOKEN in the local process and is never accepted as a tool
    argument.
    """
    try:
        return BaiduNetdiskClient.from_env().upload_file(
            local_file_path, remote_dir=remote_dir
        ).as_dict()
    except BaiduNetdiskError as exc:
        return {"status": "error", "message": str(exc)}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
