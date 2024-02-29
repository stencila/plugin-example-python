from pathlib import Path

import plugin_example_python
import pytest
from plugin_example_python import stencila_types as T
from plugin_example_python.plugin import Plugin
from plugin_example_python.testing import (
    Harness,
    HttpHarness,
    HttpTestingError,
    RPCTestingError,
    StdioHarness,
)


@pytest.fixture()
def plugin_path():
    path = Path(plugin_example_python.__file__).parent / "example.py"
    assert path.exists()
    return path


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


def test_plugin_path_exists(plugin_path: Path):
    assert plugin_path.exists()


async def test_direct_method_not_found():
    from plugin_example_python.plugin import ErrorCodes, _handle_json

    plugin = Plugin()
    request_json = {
        "jsonrpc": "2.0",
        "id": "test",
        "method": "non_existent_method",
        "params": {},
    }
    response = await _handle_json(plugin, request_json)
    assert "error" in response
    assert response["error"].get("code") == ErrorCodes.METHOD_NOT_FOUND


async def test_authentication_token_works(http_harness: HttpHarness):
    # Mess with the headers
    http_harness.headers = {"Authorization": "Bearer xxx"}
    with pytest.raises(HttpTestingError):
        await http_harness.send_rpc("health")
    http_harness.headers = {}
    with pytest.raises(HttpTestingError):
        await http_harness.send_rpc("health")


async def test_health(harness: Harness):
    res = await harness.send_rpc("health")
    assert res["status"] == "OK"


async def test_bad_json(harness: Harness):
    with pytest.raises(RPCTestingError):
        await harness.send_raw({"x": 1})


async def test_kernel(harness: Harness):
    # WIP
    result = await harness.send_rpc("kernel_start", kernel="ExampleKernel")
    assert result is not None
    ki = T.KernelInstance(**result)

    result = await harness.send_rpc("kernel_info", instance=ki.instance)

    # Will throw if it cannot reconstruct it.
    T.SoftwareApplication(**result)

    result = await harness.send_rpc("kernel_packages", instance=ki.instance)
    [T.SoftwareSourceCode(**dct) for dct in result]