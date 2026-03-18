import json
import socket

# Max buffer size before a complete header is found (1MB).
# Prevents memory exhaustion from a slow trickle of bytes without header terminators.
MAX_HEADER_ACCUMULATION = 1_048_576


class JsonRpcConnection:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = b""
        self._closed = False

    def feed(self, data: bytes):
        if not data:
            self._closed = True
            return
        self._buffer += data

    def try_read_message(self) -> dict | None:
        header_end = self._buffer.find(b"\r\n\r\n")
        if header_end < 0:
            if len(self._buffer) > MAX_HEADER_ACCUMULATION:
                self._closed = True
                self._buffer = b""
                raise ValueError("Header accumulation limit exceeded")
            return None
        try:
            header = self._buffer[:header_end].decode('ascii')
        except UnicodeDecodeError:
            self._closed = True
            self._buffer = self._buffer[header_end + 4:]
            raise ValueError("Non-ASCII header data")
        content_length = None
        for line in header.split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    self._closed = True
                    self._buffer = self._buffer[header_end + 4:]
                    raise ValueError(f"Invalid Content-Length header: {line}")
        if content_length is None:
            self._closed = True
            self._buffer = self._buffer[header_end + 4:]
            raise ValueError("Missing Content-Length header")
        if content_length < 0:
            self._closed = True
            self._buffer = self._buffer[header_end + 4:]
            raise ValueError(f"Negative Content-Length: {content_length}")
        if content_length > 10_000_000:  # 10MB limit
            self._closed = True
            self._buffer = self._buffer[header_end + 4:]
            raise ValueError(f"Content-Length too large: {content_length}")
        body_start = header_end + 4
        body_end = body_start + content_length
        if len(self._buffer) < body_end:
            return None
        body = self._buffer[body_start:body_end]
        self._buffer = self._buffer[body_end:]
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Invalid JSON body: {e}")

    def send_message(self, msg: dict):
        body = json.dumps(msg).encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('ascii')
        try:
            self._sock.sendall(header + body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._closed = True

    def send_notification(self, method: str, params: dict):
        self.send_message({"jsonrpc": "2.0", "method": method, "params": params})

    def send_response(self, request_id, result: dict):
        self.send_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    def send_error(self, request_id, code: int, message: str):
        self.send_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    @property
    def is_closed(self) -> bool:
        return self._closed
