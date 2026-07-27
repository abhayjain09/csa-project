import fs from "node:fs/promises";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const workspace = "/Users/abhay/Documents/csa-project/pageindex-agentcore/.tmp/lucid-aws-infra";
const inputPptx = `${workspace}/template-starter.pptx`;
const outputPptx = "/Users/abhay/Documents/csa-project/pageindex-agentcore/exports/pageindex-aws-infrastructure-simple-light.pptx";
const diagramPath = `${workspace}/aws-infrastructure.png`;

const presentation = await PresentationFile.importPptx(await FileBlob.load(inputPptx));
const slide = presentation.slides.items[0];

const title = slide.placeholders.getItem("title");
title.text = "AWS resources power PageIndex through AgentCore";

const body = slide.shapes.items.find((shape) => shape.name === "Google Shape;534;p58");
body.text.set([
  { bulletCharacter: "", runs: [{ run: "Architecture at a glance", textStyle: { bold: true, fontSize: "24pt", typeface: "Helvetica Neue" } }] },
  {
    bulletCharacter: "•",
    marginLeft: 22,
    indent: -12,
    runs: [" ", { run: "Amazon EC2", textStyle: { bold: true } }, " discovers PDFs and invokes the runtime."],
  },
  {
    bulletCharacter: "•",
    marginLeft: 22,
    indent: -12,
    runs: [" ", { run: "AgentCore", textStyle: { bold: true } }, " pulls the ARM64 PageIndex image from Amazon ECR."],
  },
  {
    bulletCharacter: "•",
    marginLeft: 22,
    indent: -12,
    runs: [" ", { run: "The runtime", textStyle: { bold: true } }, " reads PDFs from S3 and invokes Claude through Bedrock."],
  },
  {
    bulletCharacter: "•",
    marginLeft: 22,
    indent: -12,
    runs: [" ", { run: "CloudWatch", textStyle: { bold: true } }, " receives runtime logs; IAM controls every connection."],
  },
]);
body.text.style = {
  fontSize: 22,
  typeface: "Helvetica Neue",
  color: "#000000",
  alignment: "left",
  verticalAlignment: "top",
  wrap: "square",
  autoFit: "none",
  insets: { top: 0, right: 0, bottom: 0, left: 0 },
};

const image = slide.images.items[0];
const oldFrame = image.frame;
const oldGeometry = image.geometry;
const oldBorderRadius = image.borderRadius;
const bytes = await fs.readFile(diagramPath);
const imageBuffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
image.replace({
  blob: imageBuffer,
  contentType: "image/png",
  alt: "AWS resource architecture for the PageIndex AgentCore runtime",
  fit: "contain",
});
image.frame = oldFrame;
image.geometry = oldGeometry;
image.borderRadius = oldBorderRadius;
image.fit = "contain";

const footer = slide.shapes.items.find((shape) => shape.placeholderType === "footer");
if (footer) footer.delete();

const slideNumber = slide.placeholders.getItem("slideNumber");
slideNumber.text = "1";

slide.speakerNotes.textFrame.setText(
  "[Sources]\n" +
  "- /Users/abhay/Documents/csa-project/pageindex-agentcore/infra/main.tf\n" +
  "- https://developer.lucid.co/docs/overview-si\n" +
  "- https://developer.lucid.co/docs/aws-2024-library\n" +
  "[/Sources]",
);

await fs.mkdir(`${workspace}/final-render`, { recursive: true });
const preview = await presentation.export({ slide, format: "png", scale: 2 });
await fs.writeFile(`${workspace}/final-render/slide-1.png`, new Uint8Array(await preview.arrayBuffer()));
const layout = await slide.export({ format: "layout" });
await fs.writeFile(`${workspace}/final-render/slide-1.layout.json`, await layout.text());
const montage = await presentation.export({ format: "webp", montage: true, scale: 1 });
await fs.writeFile(`${workspace}/final-render/montage.webp`, new Uint8Array(await montage.arrayBuffer()));

const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(outputPptx);

const inspect = await presentation.inspect({
  kind: "slide,textbox,shape,image,notes,layout",
  maxChars: 12000,
});
await fs.writeFile(`${workspace}/final-render/inspect.ndjson`, inspect.ndjson, "utf8");
