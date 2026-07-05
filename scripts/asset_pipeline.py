#!/usr/bin/env python3

"""Asset pipeline: generates AI image prompts and curates real assets from research."""
import json, os, sys, tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.request import urlretrieve

ASSETS_DIR = Path.home() / "workspace" / "video-pipeline" / "assets"

@dataclass
class ImagePrompt:
    segment_index: int
    prompt: str
    style: str = "editorial"
    aspect_ratio: str = "16:9"
    model: str = "xai-grok"
    seed: Optional[int] = None
    def to_dict(self): return asdict(self)

@dataclass
class Slide:
    path: str
    duration: float = 5.0
    zoom_amt: float = 0.08
    pan_direction: str = "random"
    text_overlay: str = ""
    type: str = "ai"
    prompt: Optional[str] = None
    model: Optional[str] = None
    seed: Optional[int] = None
    source_url: Optional[str] = None
    def to_dict(self): return asdict(self)

class ImagePromptGenerator:
    STYLE_PREFIXES = {
        "editorial": "Editorial illustration, clean modern design, limited color palette, high contrast, educational, professional, ",
        "data": "Clean data visualization, infographic style, dark background, clear labels, modern chart design, ",
        "historical": "Vintage photograph style, archival footage aesthetic, slight film grain, warm tones, ",
        "dramatic": "Cinematic wide shot, dramatic lighting, shallow depth of field, atmospheric, ",
        "diagram": "Clean technical diagram, isometric view, labeled parts, educational, precise linework, ",
    }
    def __init__(self, style="editorial"): self.style = style
    def generate_prompts(self, segments):
        prompts = []
        for i, seg in enumerate(segments):
            cue = seg.get("visual_cue", "")
            text = seg.get("text", "")
            emotion = seg.get("emotion_tone", "neutral")
            sp = self._select_style(cue, emotion)
            pt = self._build_prompt(text, cue, sp)
            prompts.append(ImagePrompt(segment_index=i, prompt=pt,
                style=self._infer_style_name(cue, emotion)))
        return prompts
    def _select_style(self, cue, emotion):
        cl = cue.lower()
        if any(w in cl for w in ["data","chart","graph","statistics"]): return self.STYLE_PREFIXES["data"]
        if any(w in cl for w in ["historical","archive","old","vintage"]): return self.STYLE_PREFIXET["historical"]
        if any(w in cl for w in ["diagram","mechanism","how it works"]): return self.STYLE_PREFIXET["diagram"]
        if any(w in emotion.lower() for w in ["dramatic","intense","surprising"]): return self.STYLE_PREFIXES["dramatic"]
        return self.STYLE_PREFIXET["editorial"]
    def _infer_style_name(self, cue, emotion):
        cl = cue.lower()
        if any(w in cl for w in ["data","chart"]): return "data"
        if any(w in cl for w in ["historical"]): return "historical"
        if any(w in cl for w in ["diagram"]): return "diagram"
        if any(w in emotion.lower() for w in ["dramatic"]): return "dramatic"
        return "editorial"
    def _build_prompt(self, text, cue, sp):
        kp = text[:100].rsplit(".",1)[0] if "." in text[:100] else text[:100]
        if len(kp)>100: kp=kp[:97]+"..."
        p = sp
        if cue: p += f"cues, "
        p += f"illustrating: {kp}, Vox explainer style, YouTube educational quality, no text labels"
        return p


class AssetCurator:
    def __init__(self, output_dir=None):
        self.output_dir = Path(output_dir) if output_dir else ASSETS_DIR / "curated"
        self.output_dir.mkdir(parents=True, exist_ok=True)
    def curate_visual_leads(self, visual_leads):
        slides = []
        for i, lead in enumerate(visual_leads):
            url = lead.get("url","")
            if not url: continue
            lp = self._download_image(url, f"real_{i:04d}")
            if not lp: continue
            caption = lead.get("caption","")
            dur = self._estimate_duration(caption)
            pan = self._infer_pan_direction(lead.get("highlight",""))
            slides.append(Slide(path=str(lp), duration=dur, zoom_amt=0.06,
                pan_direction=pan, text_overlay=caption[:80] if caption else "",
                type="real", source_url=url))
        return slides
    def _download_image(self, url, prefix="asset"):
        try:
            ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
            if ext.lower() not in (".jpg",".jpeg",".png",".webp",".gif"): ext=".jpg"
            fn = f"{prefix}{ext}"; dest = self.output_dir / fn
            if dest.exists(): return dest
            urlretrieve(url, dest)
            return dest
        except Exception as e:
            print(f"[Assets] Failed to download {url}: {e}", file=sys.stderr)
            return None
    def _estimate_duration(self, caption):
        return round(max(3.0, min(12.0, len(caption.split())/2.5)), 1)
    def _infer_pan_direction(self, highlight):
        h = highlight.lower()
        if "left" in h: return "left"
        if "right" in h: return "right"
        if "top" in h or "up" in h: return "up"
        if "bottom" in h or "down" in h: return "down"
        return "random"

class AssetPipeline:
    def __init__(self, output_dir=None, style="editorial"):
        self.output_dir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="vp_assets_"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_gen = ImagePromptGenerator(style=style)
        self.curator = AssetCurator(output_dir=str(self.output_dir / "curated"))
    def run(self, segments, visual_leads=None, generate_images=False):
        visual_leads = visual_leads or []
        prompts = self.prompt_gen.generate_prompts(segments)
        pf = self.output_dir / "image_prompts.json"
        with open(pf, "w") as f: json.dump([p.to_dict() for p in prompts], f, indent=2)
        real_slides = self.curator.curate_visual_leads(visual_leads)
        slides = []; ri = 0
        for i, seg in enumerate(segments):
            ss = seg.get("start_time", i*5.0); se = seg.get("end_time", ss+5.0)
            dur = se - ss
            if ri < len(real_slides):
                rf = max(1, len(segments)//max(1,len(real_slides)))
                if i % rf == 0:
                    slide = real_slides[ri]
                    slide.duration = dur if dur>0 else slide.duration
                    slides.append(slide.to_dict()); ri += 1; continue
            prompt = prompts[i] if i<len(prompts) else None
            slides.append(Slide(path="", duration=dur if dur>0 else 5.0,
                zoom_amt=0.08, pan_direction="random",
                text_overlay=self._extract_key_phrase(seg.get("text","")),
                type="ai", prompt=prompt.prompt if prompt else None,
                model=prompt.model if prompt else None,
                seed=prompt.seed if prompt else None).to_dict())
        return slides
    def _extract_key_phrase(self, text, max_len=80):
        if not text: return ""
        s = text.split(".")[0].strip()
        if len(s)<=max_len: return s
        words=s.split(); r=""
        for w in words:
            if len(r)+len(w)+1<=max_len: r=(r+" "+w).strip() if r else w
            else: break
        return r

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Asset pipeline test")
    p.add_argument("--segments","-s",default=None,help="JSON file with script segments")
    p.add_argument("--visual-leads","-v",default=None,help="JSON file with visual leads")
    p.add_argument("--output-dir","-o",default=None)
    args = p.parse_args()
    if args.segments:
        with open(args.segments) as f: segments=json.load(f)
    else:
        segments=[{"text":"The Electoral College is a carefully designed system.","visual_cue":"Split screen","emotion_tone":"curious","start_time":0.0,"end_time":8.0}]
    visual_leads=[]
    if args.visual_leads:
        with open(args.visual_leads) as f: visual_leads=json.load(f)
    pipeline=AssetPipeline(output_dir=args.output_dir)
    slides=pipeline.run(segments, visual_leads)
    print(json.dumps({"slides":slides,"count":len(slides)},indent=2))
