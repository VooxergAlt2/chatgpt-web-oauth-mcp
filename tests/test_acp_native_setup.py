"""Optional pinned-native setup regression; never sends a model prompt.

Run explicitly with OPSS_ACP_TEST_NATIVE_HELPER=/path/to/localharness_external.
The helper's only network is the production private namespace. A local sentinel
detects unexpected inference connections; no invocation event is sent.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_network import (
    AcpNetworkGateway, NATIVE_SKILLS_DIRECTORY, prepare_initialization,
)
from chatgpt_web_oauth_mcp.acp_sandbox import AcpSandbox
from chatgpt_web_oauth_mcp.acp_seccomp import helper_filter_fd
from chatgpt_web_oauth_mcp.acp_workspace import inspect_regular_workspace


# A stdlib-only WebSocket client runs in the same production mount namespace.
# It initializes the pinned native helper with the private artifact selector,
# then tears it down. Native skill materialization is lazy until the first turn;
# this test checks protocol compatibility without an auth runtime or model call.
_PROBE = r"""
import base64, importlib.util, json, os, pathlib, socket, struct, subprocess, sys, tempfile
spec=importlib.util.spec_from_file_location('network','/opt/acp/acp_network.py')
network=importlib.util.module_from_spec(spec); spec.loader.exec_module(network)
config=json.loads(sys.argv[1])
inference=socket.socket()
inference.bind(('127.0.0.1',0)); inference.listen(1); inference.settimeout(.1)
config['config']['models'][0]['geminiApiEndpoint']['baseUrl']='http://127.0.0.1:'+str(inference.getsockname()[1])
def exact(stream, size):
    result=b''
    while len(result)<size:
        chunk=stream.read(size-len(result))
        if not chunk: raise AssertionError('unexpected EOF during native setup')
        result+=chunk
    return result
def ws_send(sock, opcode, payload):
    mask=os.urandom(4)
    size=len(payload)
    header=bytes([128|opcode])
    if size<126: header+=bytes([128|size])
    elif size<65536: header+=bytes([254])+struct.pack('!H',size)
    else: header+=bytes([255])+struct.pack('!Q',size)
    sock.sendall(header+mask+bytes(b^mask[i%4] for i,b in enumerate(payload)))
def ws_recv(sock, stream):
    while True:
        first, second=exact(stream,2)
        assert first&128 and not second&128
        size=second&127
        if size==126: size=struct.unpack('!H',exact(stream,2))[0]
        elif size==127: size=struct.unpack('!Q',exact(stream,8))[0]
        assert size<=2*1024*1024
        payload=exact(stream,size)
        if first&15==9:
            ws_send(sock,10,payload)
            continue
        assert first&15==1, 'expected native JSON acknowledgement'
        return json.loads(payload)
native_errors=tempfile.TemporaryFile()
process=subprocess.Popen(['/usr/bin/python3','-I','-B','/opt/acp/acp_network.py','--native'],
    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=native_errors,close_fds=True)
sock=None; stream=None
try:
    directory=b'/tmp/native-conversations'
    pathlib.Path(directory.decode()).mkdir()
    process.stdin.write(network.frame(b'\x0a'+network._encode_varint(len(directory))+directory+b'\x10\x00'))
    process.stdin.flush()
    size=struct.unpack('<I',exact(process.stdout,4))[0]
    assert 1<=size<=4096
    port,key=network.decode_native_output(exact(process.stdout,size))
    import time
    for attempt in range(30):
        try:
            sock=socket.create_connection(('127.0.0.1',port),timeout=5)
            break
        except ConnectionRefusedError:
            if attempt==29: raise
            time.sleep(.05)
    nonce=base64.b64encode(os.urandom(16)).decode()
    request=('GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n'
             'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
             'Sec-WebSocket-Key: '+nonce+'\r\nx-goog-api-key: '+key+'\r\n\r\n')
    sock.sendall(request.encode())
    stream=sock.makefile('rb')
    assert stream.readline().startswith(b'HTTP/1.1 101')
    for _ in range(32):
        if stream.readline()==b'\r\n': break
    else: raise AssertionError('oversized websocket handshake')
    ws_send(sock,1,json.dumps(config).encode())
    try:
        ack=ws_recv(sock,stream)
    except Exception:
        native_errors.seek(0)
        sys.stderr.write(native_errors.read(4096).decode(errors='replace'))
        raise
    assert 'initializeConversationResponse' in ack, sorted(ack)
    assert not pathlib.Path('skills/system/run-workflow/SKILL.md').exists()
    try:
        attempted,_=inference.accept()
    except socket.timeout:
        pass
    else:
        attempted.close()
        raise AssertionError('native initialization attempted an inference request')
    print(json.dumps({'initialized':True,'workspace_skill_exists':False,
                      'inference_connections':0}),flush=True)
finally:
    inference.close()
    if stream is not None: stream.close()
    if sock is not None: sock.close()
    process.stdin.close()
    process.terminate()
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=3)
    process.stdout.close()
    native_errors.close()
"""


@pytest.mark.skipif(
    not os.environ.get("OPSS_ACP_TEST_NATIVE_HELPER"),
    reason="explicit pinned native helper required",
)
def test_native_initialization_accepts_private_skills_without_workspace_mutation(tmp_path):
    helper = Path(os.environ["OPSS_ACP_TEST_NATIVE_HELPER"])
    if not helper.is_file() or not Path("/usr/bin/bwrap").is_file():
        pytest.fail("Configured native setup test requires the helper and Linux bwrap")
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    mirror = tmp_path / "mirror"
    mirror.mkdir(mode=0o700)
    (mirror / "calculator.py").write_bytes((workspace / "calculator.py").read_bytes())
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    box = AcpSandbox(Path("/bin/true"), helper, profile, workspace, control,
                     workspace_view=mirror, writable_workspace=True)
    original_source = inspect_regular_workspace(workspace)
    original_mirror = inspect_regular_workspace(mirror)
    event = {"config": {
        "models": [{"name": "gemini-3.8-flash", "types": ["MODEL_TYPE_TEXT"],
                    "geminiApiEndpoint": {"baseUrl": "http://127.0.0.1:9", "apiKey": "offline-fixture",
                                         "httpHeaders": {"X-ACP-Proxy-Token": "a" * 32}}}],
        "workspaces": [{"filesystemWorkspace": {"directory": str(workspace)}}],
        "tools": [], "appDataDir": "/tmp/native-app-data",
        "harnessSideTools": {"runWorkflow": {"enabled": True}},
    }}
    prepared, _ = prepare_initialization(json.dumps(event), 9)
    assert json.loads(prepared)["config"]["skillsConfig"]["materializedSkillsDir"] == NATIVE_SKILLS_DIRECTORY

    async def scenario():
        gateway = AcpNetworkGateway(control / "network")
        await gateway.start()
        policy_fd = helper_filter_fd()
        process = None
        try:
            argv = box.helper_command()
            argv = [argv[0], "--seccomp", str(policy_fd), *argv[1:argv.index("--")],
                    "--", "/usr/bin/python3", "-I", "-B", "-c", _PROBE, prepared]
            process = await asyncio.create_subprocess_exec(
                *argv, pass_fds=(policy_fd,), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(process.communicate(), 20)
            assert process.returncode == 0, err.decode()
            report = json.loads(out)
            assert report == {"initialized": True, "workspace_skill_exists": False,
                              "inference_connections": 0}
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            os.close(policy_fd)
            await gateway.close()
    asyncio.run(scenario())
    assert inspect_regular_workspace(workspace) == original_source
    assert inspect_regular_workspace(mirror) == original_mirror
