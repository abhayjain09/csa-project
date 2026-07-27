import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const presentation = await PresentationFile.importPptx(
  await FileBlob.load("/Users/abhay/Documents/csa-project/pageindex-agentcore/.tmp/lucid-aws-infra/template-starter.pptx"),
);
for (const query of [
  "slide.shapes.delete shape.delete remove shape",
  "image.replace blob contentType crop frame",
  "speakerNotes textFrame setText",
]) {
  const result = presentation.help("*", {
    search: query,
    include: ["index", "examples", "notes"],
    maxChars: 10000,
  });
  console.log(`QUERY: ${query}\n${result.ndjson}\n`);
}

const footer = presentation.resolve("sh/xgz6t076");
const image = presentation.resolve("im/x8z6lone");
const slide = presentation.resolve("sl/4fe4y1");
const notes = presentation.resolve("nt/4fe4y1");
for (const [label, value] of [["footer", footer], ["image", image], ["slide", slide], ["slide.shapes", slide.shapes], ["notes", notes]]) {
  let proto = value;
  const keys = new Set();
  for (let i = 0; i < 5 && proto; i += 1) {
    for (const key of Reflect.ownKeys(proto)) keys.add(String(key));
    proto = Object.getPrototypeOf(proto);
  }
  console.log(label, [...keys].sort().join(", "));
}
