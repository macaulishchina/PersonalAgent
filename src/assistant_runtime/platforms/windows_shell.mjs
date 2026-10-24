import { spawn } from "node:child_process";

// Keep Pi's native shell tool schema/output handling; replace only OS execution.
export function windowsShellOperations(shell) {
  return {
    async exec(command, cwd, { onData, signal, timeout, env }) {
      if (signal?.aborted) throw new Error("aborted");
      if (
        timeout !== undefined &&
        (!Number.isFinite(timeout) || timeout <= 0 || timeout > 2147483.647)
      ) {
        throw new Error("Invalid shell timeout");
      }
      const python = process.env.ASSISTANT_RUNTIME_PYTHON;
      if (!python)
        throw new Error(
          "Missing application Python for Windows shell ownership",
        );
      const child = spawn(
        python,
        ["-m", "assistant_runtime.platforms.windows_shell"],
        {
          cwd,
          env: env ?? process.env,
          stdio: ["pipe", "pipe", "pipe"],
          windowsHide: true,
        },
      );
      let timedOut = false;
      let timer;
      const abort = () => child.stdin.end();
      child.stdin.on("error", () => {});
      child.stdout.on("data", onData);
      child.stderr.on("data", onData);
      const closed = new Promise((resolve, reject) => {
        child.once("error", reject);
        child.once("close", (code) => resolve(code));
      });
      try {
        child.stdin.write(JSON.stringify({ shell, command }) + "\n");
        signal?.addEventListener("abort", abort, { once: true });
        if (signal?.aborted) abort();
        if (timeout !== undefined) {
          timer = setTimeout(() => {
            timedOut = true;
            abort();
          }, timeout * 1000);
        }
        // The helper reports exit only after its Job Object and pipes settle.
        const code = await closed;
        if (signal?.aborted) throw new Error("aborted");
        if (timedOut) throw new Error(`timeout:${timeout}`);
        return { exitCode: code ?? 1 };
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        child.stdin.end();
      }
    },
  };
}
