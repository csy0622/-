import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import type { ImageProcessor } from "../tools/image-processor.ts";
import { createPhotonImages } from "./index.ts";

/**
 * `createPhotonImages()` with Photon's WebAssembly read from the installed `@silvia-odwyer/photon`, for Node and Bun.
 * A single-file binary cannot resolve it: read its own copy of `photon_rs_bg.wasm` and call `createPhotonImages()`.
 */
export async function createNodePhotonImages(): Promise<ImageProcessor> {
	const path = createRequire(import.meta.url).resolve("@silvia-odwyer/photon/photon_rs_bg.wasm");
	return createPhotonImages(await readFile(path));
}
