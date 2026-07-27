import fs from "node:fs/promises";
import { instance } from "/Users/abhay/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@viz-js/viz/dist/viz.js";

const dot = await fs.readFile(
  "/Users/abhay/Documents/csa-project/pageindex-agentcore/.tmp/lucid-aws-infra/aws-infrastructure.dot",
  "utf8",
);
const viz = await instance();
const svg = viz.renderString(dot, { engine: "dot", format: "svg" });
await fs.writeFile(
  "/Users/abhay/Documents/csa-project/pageindex-agentcore/.tmp/lucid-aws-infra/aws-infrastructure.svg",
  svg,
  "utf8",
);
