"""Self-hosted grounding server: Perceptron Isaac-0.2-1B (open weights, CC-BY-NC) on a local GPU.

Same job as the Perceptron API's detect(): image + object names -> one box per name, on a 0..1000 grid.
~0.6 s per object on an RTX 5090 after warm-up, 2.5 GB VRAM, no API quota.

    # on the GPU box (separate venv: torch cu128, transformers==4.56.2 for the model's remote code, fastapi, uvicorn)
    HF_HOME=/data/picky/hf python -m arms.robot.so101.vlm_server --host 100.93.119.54 --port 8765
    # on the robot machine
    export PICKY_VLM_URL=http://100.93.119.54:8765

Bind to the tailnet address, not 0.0.0.0: there is no auth.
"""

# No `from __future__ import annotations`: FastAPI must see the real DetectRequest class, not a string.
import argparse
import base64
import io
import re
import threading
import time

BOX_RE = re.compile(r'<point_box mention="([^"]*)">\s*\((\d+),\s*(\d+)\)\s*\((\d+),\s*(\d+)\)\s*</point_box>')
MODEL_ID = "PerceptronAI/Isaac-0.2-1B"


def parse_boxes(text: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """All `<point_box mention="..."> (x1,y1) (x2,y2) </point_box>` spans, coordinates on a 0..1000 grid."""
    return [(m.group(1), tuple(int(m.group(i)) for i in range(2, 6))) for m in BOX_RE.finditer(text)]


class Grounder:
    def __init__(self, model_id: str = MODEL_ID):
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor

        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            model_id, trust_remote_code=True, vision_attn_implementation="sdpa"
        )
        self.model = model.to(device="cuda", dtype=torch.bfloat16).eval()
        self.lock = threading.Lock()  # one generation at a time on the GPU

    def find(self, image, name: str) -> tuple[tuple[int, int, int, int] | None, str]:
        messages = [
            {"role": "user", "content": "<hint>BOX</hint>"},
            {"role": "user", "content": "<image>"},
            {"role": "user", "content": f"Find the {name}."},
        ]
        text = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(text=text, images=[image], return_tensors="pt")
        with self.lock, self.torch.inference_mode():
            out = self.model.generate(
                tensor_stream=inputs["tensor_stream"].to("cuda"),
                max_new_tokens=96,
                do_sample=False,
                pad_token_id=self.processor.tokenizer.eos_token_id,
            )
        decoded = self.processor.tokenizer.decode(out[0], skip_special_tokens=False)
        answer = decoded.rsplit("</think>", 1)[-1]
        boxes = parse_boxes(answer)
        return (boxes[0][1] if boxes else None), answer.strip()


def build_app(grounder: Grounder):
    from fastapi import FastAPI
    from PIL import Image
    from pydantic import BaseModel

    class DetectRequest(BaseModel):
        image_jpeg_b64: str
        classes: list[str]

    app = FastAPI(title="picky-grounding")

    @app.get("/health")
    def health():
        return {"ok": True, "model": MODEL_ID}

    @app.post("/detect")
    def detect(req: DetectRequest):
        image = Image.open(io.BytesIO(base64.b64decode(req.image_jpeg_b64))).convert("RGB")
        t0 = time.perf_counter()
        boxes, raw = {}, {}
        for name in req.classes:
            boxes[name], raw[name] = grounder.find(image, name)
        return {"boxes": boxes, "raw": raw, "ms": round((time.perf_counter() - t0) * 1000)}

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="bind address; use the tailnet IP to serve the robot machine")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    import uvicorn
    from PIL import Image

    t0 = time.perf_counter()
    grounder = Grounder()
    grounder.find(Image.new("RGB", (640, 480), (200, 200, 200)), "object")  # warm-up (first call ~15 s)
    print(f"model ready in {time.perf_counter() - t0:.0f}s; serving on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(build_app(grounder), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
