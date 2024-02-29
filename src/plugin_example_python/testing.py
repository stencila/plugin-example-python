"""Test harnesses for simplifying plugin testing."""
import asyncio
import json
import secrets
import socket
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import aiohttp
from aiohttp import web

# TODO: Remove this dependency?
from plugin_example_python.plugin import JSONDict

if TYPE_CHECKING:
    from asyncio.subprocess import Process


def _find_available_port() -> int:
    """Find an available port on the local machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))  # Bind to an available port provided by the OS
        s.listen(1)
        port = s.getsockname()[1]  # Retrieve the port number
        return port


def _generate_random_token() -> str:
    """Generate a secure random token."""
    token = secrets.token_hex(16)
    return token


class HttpTestingError(Exception):
    """HTTP errors when testing."""


# TODO: Actually put the codes in here.
class RPCTestingError(Exception):
    """RPC Errors during testing."""


class Harness(ABC):
    """Base class for test harnesses."""

    def __init__(self):
        """Create a new Harness."""
        self.next_id = 1

    @abstractmethod
    async def send_raw(self, payload: JSONDict) -> JSONDict | list[JSONDict]:
        """Make a raw request to the plugin (for testing)."""
        ...

    async def send_rpc(
        self,
        method: str,
        **kwargs,  # noqa: ANN003
    ) -> JSONDict | list[JSONDict]:
        """Make an RPC request to the plugin.

        Args:
            method (str): Name of the method to call.
            params (ParamsType, optional): Parameters to pass to the method.

        Returns:
            JSONDict: The result of the RPC call (if successful).
        """
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": kwargs,
            "id": self.next_id,
        }
        self.next_id += 1
        return await self.send_raw(payload)

    def _process_response(self, response: JSONDict, payload: JSONDict) -> JSONDict:
        if "error" in response:
            raise RPCTestingError(response["error"]["message"])
        if response["id"] != payload["id"]:
            raise RPCTestingError("Response ID does not match request ID")
        return response["result"]


class StdioHarness(Harness):
    """Test harness for Stdio Transport."""

    def __init__(self, path: Path, timeout: float = 1.0):
        """Create a new StdioHarness.

        Args:
            path (Path): The path to a runnable python file that will start
                the plugin.
            timeout (float): How long to wait for a response.
        """
        super().__init__()
        self.path = path
        self.timeout = timeout
        self.process: Optional[Process] = None

    async def __aenter__(self):
        env = {"STENCILA_TRANSPORT": "stdio"}

        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            self.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        if self.process.returncode is not None:
            raise RuntimeError(
                f"Plugin process exited with code {self.process.returncode}"
            )
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        if exc_type is not None:
            # Deal with this?
            _ = f"exc_type: {exc_type}, exc: {exc}, tb: {tb}"
        if self.process:
            self.process.terminate()
            await self.process.wait()

    async def send_raw(self, payload: JSONDict) -> JSONDict:  # noqa: D102
        request_str = json.dumps(payload)
        response_str = await self._send_str(request_str)
        return self._process_response(json.loads(response_str), payload)

    async def _send_str(self, request: str) -> str:
        await self._send(request)
        return await self._receive()

    async def _send(self, request: str) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("cannot send on stdin")

        self.process.stdin.write(request.encode() + b"\n")
        await self.process.stdin.drain()

    async def _receive(self) -> str:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("cannot recieve on stdout")

        response_line = await asyncio.wait_for(
            self.process.stdout.readline(), timeout=self.timeout
        )
        if not response_line:
            # If readline returns an empty string, it means the stream was
            # closed. Try seeing if stderr has some info (wrong path for
            # example).
            if self.process.stderr is None:
                error_line = b""
            else:
                error_line = await self.process.stderr.readline()

            if error_line:
                err = "Error from subprocess:" + error_line.decode()
            else:
                err = "No response from plugin, possibly crashed."
            raise RuntimeError(err)
        return response_line.decode()


class HttpHarness(Harness):
    """Test harness for Http Transport."""

    def __init__(
        self, path: Path, port: Optional[int] = None, token: Optional[str] = None
    ):
        """Create a new HttpHarness."""
        super().__init__()
        if port is None:
            port = _find_available_port()
        if token is None:
            token = _generate_random_token()
        self.path = path
        self.port = port
        self.token = token
        self.base_url = f"http://localhost:{self.port}"
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.process: Optional[Process] = None
        self.session: Optional[aiohttp.ClientSession] = None

        self.next_id = 1

    async def __aenter__(self):
        env = {
            "STENCILA_TRANSPORT": "http",
            "STENCILA_PORT": str(self.port),
            "STENCILA_TOKEN": self.token,
        }

        # Start the process.
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            self.path,
            env=env,
        )
        # Give the plugin some time to start.
        await asyncio.sleep(0.2)
        if self.process.returncode is not None:
            raise RuntimeError(
                f"Plugin process exited with code {self.process.returncode}"
            )

        # Start the session.
        self.session = aiohttp.ClientSession()
        return self

    async def send_raw(self, payload: JSONDict) -> JSONDict:
        """Make a raw request to the plugin (for testing)."""
        if self.session is None:
            raise RuntimeError("Session not initialized (use `async with`)")

        async with self.session.post(
            self.base_url, json=payload, headers=self.headers
        ) as response:
            if response.status != web.HTTPOk.status_code:
                raise HttpTestingError(
                    f"Request failed with status code {response.status}"
                )
            resp = await response.json()

        return self._process_response(resp, payload)

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        if exc_type is not None:
            # Deal with this?
            _ = f"exc_type: {exc_type}, exc: {exc}, tb: {tb}"

        if self.session:
            await self.session.close()
            self.session = None

        if self.process:
            self.process.terminate()
            await self.process.wait()
            self.process = None


try:
    # WIP: This is not working yet.
    import pytest

    @pytest.fixture()
    async def stdio_harness(plugin_path: Path):
        async with StdioHarness(plugin_path) as harness:
            yield harness

    @pytest.fixture()
    async def http_harness(plugin_path: Path):
        async with HttpHarness(plugin_path) as harness:
            yield harness

    @pytest.fixture(params=["stdio_harness", "http_harness"])
    def harness(request):  # noqa: ANN001
        return request.getfixturevalue(request.param)

except ImportError:
    pass
