// Keep the npm wrapper's binary discovery, environment, signals and exit status.
import { pathToFileURL } from "node:url";

const [, , title, entrypoint, ...args] = process.argv;
process.title = title;
process.argv = [process.execPath, entrypoint, ...args];
await import(pathToFileURL(entrypoint).href);
