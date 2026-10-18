import { connection } from "next/server";
import { parseMemoryInput } from "@/lib/memory/model";
import { memoryRepository } from "@/lib/memory/server";

function sameOrigin(req: Request) {
  const origin = req.headers.get("origin");
  return !origin || origin === new URL(req.url).origin;
}

export async function GET() {
  await connection();
  try {
    return Response.json({ entries: await memoryRepository.list() }, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return Response.json({ error: "Saved information could not be loaded. Try again." }, { status: 503 });
  }
}

export async function POST(req: Request) { return write(req, "add"); }
export async function PATCH(req: Request) { return write(req, "update"); }
export async function DELETE(req: Request) { return write(req, "remove"); }

async function write(req: Request, action: "add" | "update" | "remove") {
  if (!sameOrigin(req)) return Response.json({ error: "Save information from this app only." }, { status: 403 });
  let body: Record<string, unknown>;
  try {
    const raw = await req.text();
    if (raw.length > 12000) return Response.json({ error: "That note is too long." }, { status: 413 });
    body = JSON.parse(raw);
    if (!body || typeof body !== "object") throw new Error("Invalid request.");
    if (action !== "add" && (typeof body.id !== "string" || body.id.length > 80)) throw new Error("Choose a saved note.");
    if (action !== "remove") parseMemoryInput(body);
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "Check your note and try again." }, { status: 400 });
  }
  try {
    if (action === "add") return Response.json({ entry: await memoryRepository.add(body) }, { status: 201 });
    if (action === "update") return Response.json({ entry: await memoryRepository.update(body.id as string, body) });
    await memoryRepository.remove(body.id as string);
    return Response.json({ ok: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (message.startsWith("That note") || message.startsWith("Your saved information")) return Response.json({ error: message }, { status: 409 });
    return Response.json({ error: "Couldn't save the change. Your previous information is still on file. Try again." }, { status: 503 });
  }
}
