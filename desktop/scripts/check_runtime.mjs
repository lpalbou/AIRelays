// Guards release builds: an installer without the embedded relay runtime is
// broken for end users (the app would silently fall back to PATH lookup),
// and a STALE runtime ships yesterday's relay behind today's UI.
import { existsSync, statSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const runtimeDir = join(here, "..", "src-tauri", "runtime");
const repoSrc = join(here, "..", "..", "src");

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

// Staleness: newest source file in the repo's relay packages must not be
// newer than the embedded install.
function newestMtime(dir) {
  let newest = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory() && entry.name !== "__pycache__") {
      newest = Math.max(newest, newestMtime(path));
    } else if (entry.name.endsWith(".py")) {
      newest = Math.max(newest, statSync(path).mtimeMs);
    }
  }
  return newest;
}

const embeddedMarkers = [
  join(runtimeDir, "lib", "python3.13", "site-packages", "airelay", "cli.py"),
  join(runtimeDir, "Lib", "site-packages", "airelay", "cli.py"),
].filter(existsSync);

if (embeddedMarkers.length > 0 && existsSync(repoSrc)) {
  const embeddedAt = statSync(embeddedMarkers[0]).mtimeMs;
  if (newestMtime(repoSrc) > embeddedAt) {
    console.error(
      "ERROR: the embedded relay runtime is OLDER than the relay sources in src/.\n" +
        "Re-run scripts/bundle_runtime.sh before building, or `npm run build:nocheck` " +
        "to knowingly ship the stale runtime."
    );
    process.exit(1);
  }
}
