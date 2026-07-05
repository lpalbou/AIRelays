// Guards release builds: an installer without the embedded relay runtime is
// broken for end users (the app would silently fall back to PATH lookup).
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const runtimeDir = join(dirname(fileURLToPath(import.meta.url)), "..", "src-tauri", "runtime");
const candidates = [
  join(runtimeDir, "bin", "python3"),
  join(runtimeDir, "bin", "python3.13"),
  join(runtimeDir, "python.exe"),
];

if (!candidates.some(existsSync)) {
  console.error(
    "ERROR: no embedded relay runtime found in src-tauri/runtime/.\n" +
      "Run scripts/bundle_runtime.sh first, or use `npm run build:nocheck` for a dev build."
  );
  process.exit(1);
}
