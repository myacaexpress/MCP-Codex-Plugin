"""Minimal, dependency-free MCP server over stdio (JSON-RPC 2.0, newline-delimited).

Implements the subset Codex and other MCP clients use: initialize, ping, tools/list,
tools/call (text + structuredContent), empty resources/prompts listings, cancellation
notifications. Protocol versions 2024-11-05, 2025-03-26 and 2025-06-18 are accepted.
Only JSON-RPC messages are written to stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import inspect
import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """Expected, user-facing tool failure (bad input, unavailable source)."""


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., Any]
    input_schema: dict
    title: str = ""
    annotations: dict = field(default_factory=dict)


class Server:
    def __init__(self, name: str, version: str, instructions: str = ""):
        self.name, self.version, self.instructions = name, version, instructions
        self.tools: dict[str, Tool] = {}
        self._out = sys.stdout
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4)
        self.protocol_version = SUPPORTED_VERSIONS[0]

    def tool(self, name: str, description: str, schema: dict, title: str = "", read_only: bool = True,
             open_world: bool = True):
        def deco(func):
            self.tools[name] = Tool(name, description.strip(), func, schema, title or name.replace("_", " ").title(),
                                    {"readOnlyHint": read_only, "openWorldHint": open_world,
                                     "destructiveHint": False, "idempotentHint": read_only})
            return func
        return deco

    # ------------------------------------------------------------------ io
    def _send(self, msg: dict):
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._write_lock:
            self._out.write(data + "\n")
            self._out.flush()

    def _result(self, mid, result):
        self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    def _error(self, mid, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"jsonrpc": "2.0", "id": mid, "error": err})

    # ------------------------------------------------------------------ validation
    @staticmethod
    def _validate(schema: dict, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ToolError("Tool arguments must be a JSON object.")
        props = schema.get("properties", {})
        unknown = set(args) - set(props)
        if unknown and schema.get("additionalProperties") is False:
            raise ToolError(f"Unknown argument(s): {sorted(unknown)}. Allowed: {sorted(props)}")
        for req in schema.get("required", []):
            if req not in args or args[req] in (None, ""):
                raise ToolError(f"Missing required argument '{req}'.")
        type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list,
                    "object": dict}
        clean = {}
        for key, value in args.items():
            if key not in props:
                continue
            spec = props[key]
            if value is None:
                continue
            t = spec.get("type")
            types = t if isinstance(t, list) else [t] if t else []
            ok = not types or any(
                isinstance(value, type_map[x]) and not (x in ("integer", "number") and isinstance(value, bool))
                for x in types if x in type_map)
            if not ok and "integer" in types and isinstance(value, str) and value.strip().isdigit():
                value, ok = int(value), True
            if not ok and "boolean" in types and isinstance(value, str) and value.lower() in ("true", "false"):
                value, ok = value.lower() == "true", True
            if not ok:
                raise ToolError(f"Argument '{key}' must be of type {t}.")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolError(f"Argument '{key}' must be one of {spec['enum']}.")
            if isinstance(value, int) and not isinstance(value, bool):
                if "minimum" in spec and value < spec["minimum"]:
                    raise ToolError(f"Argument '{key}' must be >= {spec['minimum']}.")
                if "maximum" in spec and value > spec["maximum"]:
                    raise ToolError(f"Argument '{key}' must be <= {spec['maximum']}.")
            if isinstance(value, str) and len(value) > spec.get("maxLength", 4000):
                raise ToolError(f"Argument '{key}' is too long.")
            if isinstance(value, list):
                if len(value) > spec.get("maxItems", 50):
                    raise ToolError(f"Argument '{key}' has too many items.")
                item_t = (spec.get("items") or {}).get("type")
                if item_t and not all(isinstance(v, type_map[item_t]) for v in value):
                    raise ToolError(f"Every item of '{key}' must be a {item_t}.")
            clean[key] = value
        return clean

    # ------------------------------------------------------------------ handlers
    def _call_tool(self, mid, params):
        try:
            self._call_tool_inner(mid, params)
        except Exception as err:  # last-resort guard: every request gets a response
            traceback.print_exc(file=sys.stderr)
            self._error(mid, -32603, f"Internal error: {type(err).__name__}")

    def _call_tool_inner(self, mid, params):
        if not isinstance(params, dict):
            self._error(mid, -32602, "Invalid params: 'params' must be an object.")
            return
        name = params.get("name")
        if not isinstance(name, str):
            self._error(mid, -32602, "Invalid params: 'name' must be a string.")
            return
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            self._error(mid, -32602, "Invalid params: 'arguments' must be an object.")
            return
        tool = self.tools.get(name)
        if tool is None:
            self._error(mid, -32602, f"Unknown tool: {name}")
            return
        try:
            args = self._validate(tool.input_schema, arguments or {})
            result = tool.func(**args)
            if not isinstance(result, dict):
                result = {"result": result}
            text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
            if len(text) > 400_000:
                text = text[:400_000] + "\n… [truncated; narrow the query or lower 'limit']"
            self._result(mid, {"content": [{"type": "text", "text": text}], "structuredContent": result,
                               "isError": False})
        except (ToolError, ValueError, KeyError, TypeError) as err:
            msg = str(err) or type(err).__name__
            self._result(mid, {"content": [{"type": "text", "text": f"Error: {msg}"}], "isError": True})
        except Exception as err:  # unexpected: report without crashing the server
            traceback.print_exc(file=sys.stderr)
            msg = f"{type(err).__name__}: {err}"
            self._result(mid, {"content": [{"type": "text", "text": f"Error: {msg}"}], "isError": True})

    def handle(self, msg):
        try:
            self._handle(msg)
        except Exception as err:  # never let one bad message kill the server
            traceback.print_exc(file=sys.stderr)
            mid = msg.get("id") if isinstance(msg, dict) else None
            if isinstance(msg, dict) and "id" in msg:
                self._error(mid, -32603, f"Internal error: {type(err).__name__}")

    def _handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            mid = msg.get("id") if isinstance(msg, dict) else None
            self._error(mid if isinstance(mid, (str, int)) else None, -32600, "Invalid Request")
            return
        method, mid = msg.get("method"), msg.get("id")
        if "id" in msg and not (mid is None or isinstance(mid, (str, int))) or isinstance(mid, bool):
            self._error(None, -32600, "Invalid Request: id must be a string or number.")
            return
        if method is None:
            return  # a response to something we never send
        if not isinstance(method, str):
            if "id" in msg:
                self._error(mid, -32600, "Invalid Request: method must be a string.")
            return
        params = msg.get("params")
        if params is None:
            params = {}
        is_request = "id" in msg
        if not isinstance(params, dict):
            if is_request:
                self._error(mid, -32602, "Invalid params: 'params' must be an object.")
            return
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.protocol_version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            self._result(mid, {"protocolVersion": self.protocol_version,
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": self.name, "version": self.version},
                               "instructions": self.instructions})
        elif method == "ping":
            if is_request:
                self._result(mid, {})
        elif method == "tools/list":
            self._result(mid, {"tools": [{"name": t.name, "title": t.title, "description": t.description,
                                          "inputSchema": t.input_schema, "annotations": t.annotations}
                                         for t in self.tools.values()]})
        elif method == "tools/call":
            if not is_request:
                return
            self._pool.submit(self._call_tool, mid, params)
        elif method == "resources/list":
            self._result(mid, {"resources": []})
        elif method == "resources/templates/list":
            self._result(mid, {"resourceTemplates": []})
        elif method == "prompts/list":
            self._result(mid, {"prompts": []})
        elif method.startswith("notifications/"):
            return
        elif is_request:
            self._error(mid, -32601, f"Method not found: {method}")

    def run(self):
        # Protect the protocol stream: anything printed by libraries goes to stderr.
        self._out = sys.stdout
        sys.stdout = sys.stderr
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._error(None, -32700, "Parse error")
                continue
            if isinstance(msg, list):
                if not msg:
                    self._error(None, -32600, "Invalid Request: empty batch")
                for m in msg:
                    self.handle(m)
            else:
                self.handle(msg)
        self._pool.shutdown(wait=True)


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}
