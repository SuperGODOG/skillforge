import { existsSync, readFileSync } from "node:fs";
import { resolve as pathResolve } from "node:path";
import { pathToFileURL } from "node:url";

const BRIDGE_PARENT = new URL("./package.json", import.meta.url).href;

// Optional PI_SOURCE_ROOT development mode
const piSourceRoot = process.env.PI_SOURCE_ROOT;
let sourceRootMap = null;

if (piSourceRoot) {
  const pkgPath = pathResolve(piSourceRoot, "package.json");
  if (!existsSync(pkgPath)) {
    throw new Error(`Invalid PI_SOURCE_ROOT '${piSourceRoot}': package.json not found`);
  }
  sourceRootMap = {
    "@earendil-works/pi-agent-core": pathToFileURL(pathResolve(piSourceRoot, "packages/agent/src/index.ts")).href,
    "@earendil-works/pi-ai/compat": pathToFileURL(pathResolve(piSourceRoot, "packages/ai/src/compat.ts")).href,
    "@earendil-works/pi-ai": pathToFileURL(pathResolve(piSourceRoot, "packages/ai/src/index.ts")).href,
  };
}

export async function resolve(specifier, context, nextResolve) {
  // If explicit dev mode is configured via PI_SOURCE_ROOT
  if (sourceRootMap) {
    if (sourceRootMap[specifier]) {
      return nextResolve(sourceRootMap[specifier], context);
    }
  }

  // Fallback module overrides to local package parent
  const overrides = ["typebox", "partial-json", "diff", "yaml", "ignore"];
  for (const pkg of overrides) {
    if (specifier === pkg || specifier.startsWith(`${pkg}/`)) {
      return nextResolve(specifier, {
        ...context,
        parentURL: BRIDGE_PARENT,
      });
    }
  }
  return nextResolve(specifier, context);
}
