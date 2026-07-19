import { spawn } from "node:child_process";
import process from "node:process";

const children = [
  spawn("uv", ["run", "--project", "apps/api", "game-assets-api"], {
    cwd: process.cwd(),
    stdio: "inherit",
  }),
  spawn("npm", ["--prefix", "apps/web", "run", "dev", "--", "--host", "127.0.0.1", "--port", "4173", "--strictPort"], {
    cwd: process.cwd(),
    stdio: "inherit",
  }),
];

let stopping = false;
function stop(signal = "SIGTERM") {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (!child.killed) child.kill(signal);
  }
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => stop(signal));
}

for (const child of children) {
  child.on("exit", (code) => {
    if (!stopping && code !== 0) {
      stop();
      process.exitCode = code ?? 1;
    }
  });
}
