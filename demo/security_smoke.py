"""Real Docker/SDK checks, without calling an LLM. Exit 2 means NOT VERIFIED.

Start the project's Docker Compose services before running this module. Uses
unique probe files and only removes files created by this run. No stress bomb.
"""

import json
import shutil
import subprocess
import time
import uuid

from core.config import get_settings
from sandbox.client import SandboxClient
from security.permission_manager import PermissionDenied


def main():
    if shutil.which("docker") is None:
        print("NOT VERIFIED: docker command is unavailable; no real sandbox checks ran.")
        return 2
    # docker inspect belongs to the host, not the code-execution container.
    inspect = subprocess.run(["docker", "inspect", "doc-agent-sandbox"], capture_output=True, text=True, timeout=15)
    if inspect.returncode:
        print("NOT VERIFIED: start the project's Docker Compose sandbox first.")
        return 2
    container = json.loads(inspect.stdout)[0]
    if not container["State"]["Running"]:
        print("NOT VERIFIED: sandbox container is not running.")
        return 2
    limits = container["HostConfig"]
    assert limits["Memory"] > 0 and limits["NanoCpus"] > 0 and (limits["PidsLimit"] or 0) > 0, "container quotas missing"
    assert limits["MemorySwap"] == limits["Memory"], "unexpected swap policy"
    print("PASS running container quotas:", {k: limits[k] for k in ("Memory", "NanoCpus", "PidsLimit", "MemorySwap")})
    client = SandboxClient(get_settings())
    prefix = "security-probe-" + uuid.uuid4().hex
    filename, marker = prefix + ".txt", prefix + "-late.txt"
    try:
        client.write_text_file(filename, "security probe")
        assert client.read_text_file(filename) == "security probe"
        print("PASS real file write/read")
        try:
            client.read_text_file("../outside")
            raise AssertionError("path traversal accepted")
        except PermissionDenied:
            print("PASS traversal denied")
        assert client.execute_python("security_probe_variable = 1").status == "ok"
        response = client.execute_python("print('security_probe_variable' in globals())")
        assert response.status == "ok" and response.stdout.strip() == "False"
        print("PASS independent Jupyter sessions")
        path = client.resolve(marker)
        code = f"import time\nfrom pathlib import Path\ntime.sleep(6)\nPath({path!r}).write_text('late side effect')"
        response = client.execute_python(code, timeout=1)
        assert response.status == "timeout" and response.execution_uncertain, response.text
        time.sleep(7)
        check = client.execute_python(f"from pathlib import Path\nprint(Path({path!r}).exists())", timeout=5)
        assert check.status == "ok" and check.stdout.strip() == "False", "timed-out code kept running"
        print("PASS timeout probe stopped before delayed write; this does not prove all child processes are killed")
    finally:
        for path in (filename, marker):
            client.delete_file(path)
    print("PASS real smoke checks complete. Save this output with image/SDK versions as verification evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
