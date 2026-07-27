import fs from "node:fs/promises";
import { instance } from "/Users/abhay/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@viz-js/viz/dist/viz.js";
import sharp from "/Users/abhay/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/sharp/lib/index.js";

const source = "/Users/abhay/Documents/csa-project/pageindex-agentcore/exports/pageindex-aws-full-infrastructure.dot";
const svgOutput = "/Users/abhay/Documents/csa-project/pageindex-agentcore/exports/pageindex-aws-full-infrastructure.svg";
const pngOutput = "/Users/abhay/Documents/csa-project/pageindex-agentcore/exports/pageindex-aws-full-infrastructure.png";

const dot = await fs.readFile(source, "utf8");
const viz = await instance();
const result = viz.renderString(dot, { engine: "dot", format: "svg" });
await fs.writeFile(svgOutput, result, "utf8");
await sharp(Buffer.from(result))
  .resize({ width: 3000, withoutEnlargement: false })
  .flatten({ background: "#FFFFFF" })
  .png({ compressionLevel: 9, adaptiveFiltering: true })
  .toFile(pngOutput);
