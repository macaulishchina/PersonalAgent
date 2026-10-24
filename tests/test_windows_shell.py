"""Real Windows shell cancellation must settle the entire owned process tree."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from platform_support import process_alive
from assistant_runtime.platforms import windows_shell


@unittest.skipUnless(sys.platform == "win32", "Windows Job Object shell ownership")
class WindowsShellTest(unittest.TestCase):
    def test_shells_preserve_working_directory_environment_output_and_exit_code(self):
        module = Path(windows_shell.__file__).with_suffix(".mjs").as_uri()
        for shell, command in (
            ("powershell", "[Console]::Write($env:AGENT_SHELL_TEST); Get-Content marker.txt; exit 7"),
            ("bash", 'printf "%s" "$AGENT_SHELL_TEST"; cat marker.txt; exit 7'),
        ):
            with self.subTest(shell=shell), tempfile.TemporaryDirectory(prefix="shell 空格 ") as folder:
                (Path(folder) / "marker.txt").write_text("cwd", encoding="utf-8")
                script = f"""
import {{ windowsShellOperations }} from {json.dumps(module)};
let output = '';
const result = await windowsShellOperations({json.dumps(shell)}).exec(
  {json.dumps(command)}, {json.dumps(folder)}, {{
    env: {{ ...process.env, AGENT_SHELL_TEST: '环境' }},
    onData: data => {{ output += data.toString('utf8'); }},
  }});
if (result.exitCode !== 7 || output.trim() !== '环境cwd') {{
  throw new Error(JSON.stringify({{ result, output }}));
}}
"""
                result = subprocess.run(
                    ["node", "--input-type=module", "-e", script],
                    env={**os.environ, "ASSISTANT_RUNTIME_PYTHON": sys.executable},
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_abort_and_timeout_stop_shell_descendants(self):
        module = Path(windows_shell.__file__).with_suffix(".mjs").as_uri()
        for action in ("abort", "timeout"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                marker = Path(folder) / "child.pid"
                python = sys.executable.replace("'", "''")
                destination = str(marker).replace("'", "''")
                command = (
                    f"$child = Start-Process -FilePath '{python}' "
                    "-ArgumentList @('-c', '\"import time; time.sleep(60)\"') "
                    "-WindowStyle Hidden -PassThru; "
                    f"[IO.File]::WriteAllText('{destination}', $child.Id.ToString()); "
                    "Start-Sleep -Seconds 60"
                )
                script = f"""
import {{ windowsShellOperations }} from {json.dumps(module)};
import {{ existsSync }} from 'node:fs';
const controller = new AbortController();
let timer;
if ({json.dumps(action)} === 'abort') {{
  timer = setInterval(() => {{
    if (existsSync({json.dumps(str(marker))})) {{
      clearInterval(timer);
      controller.abort();
    }}
  }}, 20);
}}
try {{
  await windowsShellOperations('powershell').exec(
    {json.dumps(command)}, {json.dumps(folder)}, {{
      signal: controller.signal,
      timeout: {2 if action == "timeout" else 10},
      onData: () => {{}},
    }});
  throw new Error('Command unexpectedly completed');
}} catch (error) {{
  if (error.message !== {json.dumps("aborted" if action == "abort" else "timeout:2")}) throw error;
}} finally {{ clearInterval(timer); }}
"""
                result = subprocess.run(
                    ["node", "--input-type=module", "-e", script],
                    env={**os.environ, "ASSISTANT_RUNTIME_PYTHON": sys.executable},
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertTrue(marker.is_file(), "The real command must start before cancellation")
                self.assertFalse(process_alive(int(marker.read_text())))
