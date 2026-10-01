"""自建 x-mcp 中继：把浏览器插件的 WebSocket 连接变成可调用的工具通道。

拓扑（透传）：
    浏览器插件 --wss--> 本服务 /xhs/ws <== 后端 POST /xhs/mcp(JSON-RPC tools/call)

插件「服务器地址」填 wss://<本服务域名>/xhs/ws，apiKey 填 XHS_RELAY_TOKEN；
后端 XHS_API_BASE=https://<本服务域名>/xhs/mcp + XHS_API_TOKEN=<同一 token>
即完成自建透传，不再依赖 mcp.aredink.com 云中继。

协议（逆向自插件 background.js）：
    插件→服务端  {type:"hello", payload:{apiKey,extensionId,extensionVersion}}
                 {type:"tool_result", responseToRequestId, payload:{status,data|error}}
                 {type:"ping"} / {type:"status"}
    服务端→插件  {type:"tool_call", requestId, payload:{name,args}}
                 {type:"pong"} / {type:"error"} / {type:"update_extension_id"}
    apiKey 校验失败用 close code 4401（插件端会显示"密钥被拒"并停连）。
"""
import asyncio
import json
import os
import uuid

RELAY_TOKEN = os.environ.get("XHS_RELAY_TOKEN", "").strip()

_ws = None            # 当前在线插件 websocket（单插件，后来者顶替）
_loop = None          # uvicorn 事件循环（跨线程投递用）
_pending = {}         # requestId -> asyncio.Future


def online() -> bool:
    return _ws is not None


async def handle_ws(ws):
    """/xhs/ws 端点主循环：收 hello 鉴权、转发 tool_result 给等待中的 Future。"""
    global _ws, _loop
    await ws.accept()
    _loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
            except Exception:
                break                                # 断开/非法帧
            t = msg.get("type")
            if t == "hello":
                key = str((msg.get("payload") or {}).get("apiKey") or "")
                if RELAY_TOKEN and key != RELAY_TOKEN:
                    try:
                        await ws.send_text(json.dumps(
                            {"type": "error",
                             "payload": {"message": "invalid api key"}}))
                    finally:
                        await ws.close(code=4401)
                    return
                _ws = ws                             # 新连接顶替旧连接
                continue
            if t == "ping":
                try:
                    await ws.send_text(json.dumps({"type": "pong"}))
                except Exception:
                    break
                continue
            if t == "tool_result":
                fut = _pending.pop(msg.get("responseToRequestId"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg.get("payload") or {})
                continue
            # notice / status / 其余类型忽略
    finally:
        if _ws is ws:
            _ws = None


async def _acall(name, args, timeout):
    rid = uuid.uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    _pending[rid] = fut
    try:
        await _ws.send_text(json.dumps({
            "type": "tool_call", "requestId": rid,
            "payload": {"name": name, "args": args or {}}}))
        res = await asyncio.wait_for(fut, timeout)
    finally:
        _pending.pop(rid, None)
    if (res or {}).get("status") != "success":
        raise RuntimeError(str((res or {}).get("error") or "插件执行失败")[:120])
    return res.get("data")


def call_tool(name, args=None, timeout=60):
    """同步入口（在线程池中调用）：经事件循环把工具调用投给插件并等结果。"""
    if _ws is None or _loop is None:
        raise RuntimeError("小红书插件未连接（浏览器开着插件并连上 /xhs/ws 后重试）")
    return asyncio.run_coroutine_threadsafe(
        _acall(name, args, timeout), _loop).result(timeout + 10)


def mcp_dispatch(body):
    """MCP Streamable HTTP 的 JSON-RPC 分发（同步）：
    支持 initialize / notifications/* / tools/list / tools/call。"""
    rid, method = body.get("id"), body.get("method") or ""
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "xhs-relay", "version": "1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": n} for n in ("check_login_status", "search_feeds",
                                  "get_feed_detail", "list_feeds",
                                  "publish_content")]}}
    if method == "tools/call":
        p = body.get("params") or {}
        try:
            data = call_tool(str(p.get("name") or ""),
                             p.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": rid, "result": data}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32000, "message": str(e)[:200]}}
    if method.startswith("notifications/"):
        return None                                   # 通知无需响应
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}
