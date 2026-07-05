#!/usr/bin/env python3
"""Researcher Agent -- Sprint 1: fact-checked web research with provenance."""
import argparse, json, re, sys, time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    from duckduckgo_search import DDGS; HAS_DDGS = True
except ImportError:
    HAS_DDGS = False
try:
    import trafilatura; HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
try:
    from bs4 import BeautifulSoup; HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RELIABILITY_TIERS = {"government":0.95,"official":0.95,"edu":0.90,"peer_reviewed":0.90,"academic":0.85,"major_news":0.75,"news":0.65,"institutional":0.70,"org":0.60,"blog":0.40,"independent":0.35,"social":0.20,"forum":0.15,"unknown":0.30}
RELIABLE_DOMAINS = {"gov":("government",0.95),"who.int":("official",0.95),"un.org":("official",0.95),"nasa.gov":("government",0.95),"nih.gov":("government",0.95),"cdc.gov":("government",0.95),"noaa.gov":("government",0.95),"epa.gov":("government",0.95),"census.gov":("government",0.95),"bls.gov":("government",0.95),"sec.gov":("government",0.95),"fda.gov":("government",0.95),"usgs.gov":("government",0.95),"edu":("edu",0.90),"mit.edu":("academic",0.90),"stanford.edu":("academic",0.90),"harvard.edu":("academic",0.90),"berkeley.edu":("academic",0.90),"ox.ac.uk":("academic",0.90),"cam.ac.uk":("academic",0.90),"reuters.com":("major_news",0.85),"apnews.com":("major_news",0.85),"bbc.com":("major_news",0.80),"bbc.co.uk":("major_news",0.80),"npr.org":("major_news",0.80),"pbs.org":("major_news",0.80),"economist.com":("major_news",0.80),"nytimes.com":("news",0.70),"washingtonpost.com":("news",0.70),"wsj.com":("news",0.70),"theguardian.com":("news",0.70),"cnn.com":("news",0.65),"bloomberg.com":("news",0.70),"ft.com":("news",0.70),"nature.com":("peer_reviewed",0.90),"science.org":("peer_reviewed",0.90),"sciencedirect.com":("peer_reviewed",0.90),"pnas.org":("peer_reviewed",0.90),"thelancet.com":("peer_reviewed",0.90),"nejm.org":("peer_reviewed",0.90),"wikipedia.org":("institutional",0.55),"britannica.com":("institutional",0.80),"worldbank.org":("official",0.90),"imf.org":("official",0.90),"oecd.org":("official",0.90)}
DEFAULT_MAX_SOURCES = 6
DEFAULT_MAX_SEARCH_RESULTS = 5
REQUEST_TIMEOUT = 12
SEARCH_SLEEP = 0.5
MAX_FACTS_PER_SOURCE = 50

@dataclass
class Fact:
    claim: str; evidence: str; source_url: str; source_name: str; date: str
    confidence: float; corroborating_sources: List[str] = field(default_factory=list)
    reliability_tier: str = "unknown"
    def to_dict(self): return asdict(self)

@dataclass
class VisualLead:
    url: str; description: str; source_url: str; image_type: str
    suggested_crop: str = ""; license_note: str = ""
    def to_dict(self): return asdict(self)

@dataclass
class ContestedFlag:
    claim: str; reason: str
    sources_for: List[str] = field(default_factory=list)
    sources_against: List[str] = field(default_factory=list)
    def to_dict(self): return asdict(self)

@dataclass
class ResearchOutput:
    topic: str; researched_at: str
    facts: List[Fact] = field(default_factory=list)
    source_summary: Dict[str, Any] = field(default_factory=dict)
    key_narrative: str = ""
    visual_leads: List[VisualLead] = field(default_factory=list)
    contested_flags: List[ContestedFlag] = field(default_factory=list)
    methodology_note: str = ""
    def to_dict(self):
        return {"topic":self.topic,"researched_at":self.researched_at,
                "facts":[f.to_dict() for f in self.facts],
                "source_summary":self.source_summary,"key_narrative":self.key_narrative,
                "visual_leads":[v.to_dict() for v in self.visual_leads],
                "contested_flags":[c.to_dict() for c in self.contested_flags],
                "methodology_note":self.methodology_note}
    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

# XML/HTML tag stripper
_TAG_RE = re.compile(r"<[^>]+>")
_XML_ENTITY_RE = re.compile(r"&\w+;")

def _clean_text(text):
    """Strip XML/HTML tags and entities, normalize whitespace."""
    text = _TAG_RE.sub(" ", text)
    text = _XML_ENTITY_RE.sub(" ", text)
    text = re.sub(r"\[edit\]", " ", text)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _create_session():
    s = requests.Session()
    retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[429,500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent":"ResearchBot/1.0","Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.5"})
    return s

def _assess_source_reliability(url, site_name=""):
    if not url: return ("unknown", RELIABILITY_TIERS["unknown"])
    domain = urlparse(url).netloc.lower().replace("www.","",1)
    for kd, (tier, score) in RELIABLE_DOMAINS.items():
        if kd in domain: return (tier, score)
    if domain.endswith(".gov"): return ("government", 0.95)
    if domain.endswith(".edu"): return ("edu", 0.90)
    if domain.endswith(".org"): return ("org", 0.60)
    if any(kw in site_name.lower() for kw in ["university","institute","research","lab"]): return ("academic",0.85)
    if any(kw in site_name.lower() for kw in ["news","times","post","journal","daily"]): return ("news",0.65)
    return ("unknown", 0.30)

def _web_search(topic, max_results=DEFAULT_MAX_SEARCH_RESULTS):
    if not HAS_DDGS: return []
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(topic, max_results=max_results):
                results.append({"title":r.get("title",""),"url":r.get("href",""),"snippet":r.get("body","")})
    except Exception as e:
        print(f"[WARN] Search error: {e}", file=sys.stderr)
    return results

def _search_x(topic):
    if not HAS_DDGS: return []
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(f"site:x.com {topic}", max_results=3):
                results.append({"title":r.get("title",""),"url":r.get("href",""),"snippet":r.get("body","")})
    except: pass
    return results

def _web_extract(url, session):
    if not HAS_TRAFILATURA: return None
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except:
        return None
    html = resp.text
    if not html or len(html) < 100: return None
    try:
        extracted = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, include_images=True, output_format="xml", with_metadata=True)
    except:
        extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
    if not extracted: return None
    title = date_str = author = text = ""
    if isinstance(extracted, str) and extracted.startswith("<doc"):
        try:
            soup = BeautifulSoup(extracted, "xml")
            title = soup.find("title").get_text(strip=True) if soup.find("title") else ""
            date_str = soup.find("date").get_text(strip=True) if soup.find("date") else ""
            author = soup.find("author").get_text(strip=True) if soup.find("author") else ""
            body = soup.find("body")
            if body:
                text = body.get_text(separator="\n", strip=True)
            else:
                text = _clean_text(extracted)
        except:
            text = trafilatura.extract(html, include_comments=False, include_tables=True)
            if text: text = _clean_text(text)
    else:
        text = extracted
    if not text or len(text.strip()) < 100: return None
    images = _extract_images(html, url)
    if not date_str: date_str = _extract_date_from_html(html)
    if not title:
        try:
            soup = BeautifulSoup(html, "lxml")
            tt = soup.find("title")
            title = tt.get_text(strip=True) if tt else url
        except: title = url
    return {"url":url,"text":text.strip(),"title":title,"date":date_str or "unknown","author":author,"images":images,"raw_html_length":len(html),"text_length":len(text),"domain":urlparse(url).netloc}

def _extract_images(html, base_url):
    images = []
    try:
        soup = BeautifulSoup(html, "lxml")
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            alt = img.get("alt","")
            if not src: continue
            skip = ["icon","logo","avatar","pixel","spacer","tracking","1x1","blank","button","badge","sprite","placeholder"]
            if any(p in src.lower() or p in alt.lower() for p in skip): continue
            if src.startswith("//"): src = "https:" + src
            elif src.startswith("/"):
                p = urlparse(base_url)
                src = f"{p.scheme}://{p.netloc}{src}"
            elif not src.startswith("http"): continue
            ctx = ""
            fc = img.find_parent("figure")
            if fc:
                cap = fc.find("figcaption")
                if cap: ctx = cap.get_text(strip=True)[:300]
            if not ctx: ctx = alt[:300]
            if not ctx:
                parent = img.parent
                if parent: ctx = parent.get_text(strip=True)[:300]
            images.append({"url":src,"alt":alt[:200],"context":ctx})
            if len(images) >= 10: break
    except: pass
    return images

def _extract_date_from_html(html):
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag, attrs in [("meta",{"property":"article:published_time"}),("meta",{"name":"pubdate"}),("meta",{"name":"date"}),("meta",{"itemprop":"datePublished"}),("time",{"datetime":True})]:
            el = soup.find(tag, attrs)
            if el:
                c = el.get("content") or el.get("datetime") or ""
                if c: return c
    except: pass
    return ""

FACT_INDICATORS = [r"(\d+(?:\.\d+)?\s*%?\s*(?:million|billion|trillion|thousand|hundred)?)",r"(according to|reported by|studies show|research indicates|data shows|citing)",r"(is the|are the|was the|were the)\s+(first|largest|smallest|only|most|least|best|worst)",r"(founded in|established in|created in|built in|launched in)\s+\d{4}",r"(caused by|result of|due to|because of|led to)",r"(increased by|decreased by|rose by|fell by|grew by|declined by)\s+\d+",r"(compared to|in contrast|unlike|whereas)",r"(leads to|results in|causes|triggers|prevents|enables)"]
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# Regex to detect citation/noise patterns
_NOISE_PATTERNS = [
    r"^\s*Archived from the original",
    r"^\s*Retrieved \d",
    r"^\s*ISBN \d",
    r"^\s*New York:",
    r"^\s*Washington,?\s*D\.?C\.?:",
    r"^\s*Chicago:",
    r"^\s*University of",
    r"^\s*Vol\.\s*\d",
    r"^\s*pp\.\s*\d",
    r"^\s*doi:",
    r"^\s*\\[a-z]+\s",
    r"^\s*et al\.",
    r"^\s*Available at:",
    r"^\s*See also:",
    r"^\s*Note:",
    r"^\s*Source:",
    r"^\s*\d+\.\s*$",
    r"^\[\d+\]",
    r"\[edit\]",
]

def _is_noise(sentence):
    """Check if a sentence looks like citation noise or boilerplate."""
    s = sentence.strip()
    if len(s) < 30: return True
    for pat in _NOISE_PATTERNS:
        if re.search(pat, s, re.IGNORECASE):
            return True
    if s.startswith("(") and s.endswith(")") and len(s) < 60: return True
    return False

def _extract_facts(text, source_url, source_name, reliability_tier, reliability_score, date_str):
    if not text or len(text) < 100: return []
    facts = []
    text = _clean_text(text)
    sentences = SENTENCE_SPLIT_RE.split(text)
    count = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 40 or len(sentence) > 400: continue
        if _is_noise(sentence): continue
        ic = sum(1 for pat in FACT_INDICATORS if re.search(pat, sentence, re.IGNORECASE))
        nc = len(re.findall(r"\d+", sentence))
        if sentence.endswith("?"): continue
        if any(m in sentence.lower() for m in ["i think","i believe","in my opinion","personally","i feel","it seems","maybe","perhaps"]): continue
        fs = min(ic*0.2, 0.6) + min(nc*0.1, 0.3) + (0.1 if len(sentence)>=60 else 0)
        if fs >= 0.3:
            bc = reliability_score * 0.7
            fc = min(bc + (fs-0.3)*0.5, 1.0)
            facts.append(Fact(claim=sentence[:300],evidence=sentence[:500],source_url=source_url,source_name=source_name[:100],date=date_str,confidence=round(fc,2),corroborating_sources=[],reliability_tier=reliability_tier))
            count += 1
            if count >= MAX_FACTS_PER_SOURCE: break
    return facts

def _normalize_claim(claim):
    n = claim.lower(); n = re.sub(r"[^a-z0-9\s]","",n)
    return re.sub(r"\s+"," ",n).strip()

def _claims_are_similar(c1, c2, threshold=0.4):
    w1 = set(_normalize_claim(c1).split()); w2 = set(_normalize_claim(c2).split())
    if not w1 or not w2: return False
    return len(w1.intersection(w2))/len(w1.union(w2)) >= threshold

def _cross_verify_facts(all_facts):
    if len(all_facts) <= 1: return all_facts
    n = len(all_facts); processed = [False]*n; merged = []
    for i in range(n):
        if processed[i]: continue
        base = all_facts[i]; corr = [base.source_url]
        for j in range(i+1, n):
            if processed[j] or all_facts[j].source_url == base.source_url: continue
            if _claims_are_similar(base.claim, all_facts[j].claim):
                corr.append(all_facts[j].source_url); processed[j] = True
        bonus = min((len(set(corr))-1)*0.1, 0.3)
        base.corroborating_sources = list(set(corr))
        base.confidence = round(min(base.confidence+bonus, 1.0), 2)
        merged.append(base); processed[i] = True
    return merged

def _identify_contested_flags(facts):
    flags = []
    for f in facts:
        if len(f.corroborating_sources) <= 1 and f.confidence < 0.6:
            flags.append(ContestedFlag(claim=f.claim,reason=f"Single-source with {f.reliability_tier} source (confidence: {f.confidence})",sources_for=[f.source_url]))
        elif f.confidence < 0.4:
            flags.append(ContestedFlag(claim=f.claim,reason=f"Low confidence ({f.confidence})",sources_for=f.corroborating_sources))
        elif f.reliability_tier in ("blog","social","forum","independent","unknown"):
            flags.append(ContestedFlag(claim=f.claim,reason=f"Source reliability concern (tier: {f.reliability_tier})",sources_for=[f.source_url]))
    return flags

def _generate_key_narrative(topic, facts):
    if not facts: return f"No reliable information found for '{topic}'."
    top = sorted(facts, key=lambda f: f.confidence, reverse=True)[:5]
    claims = [f.claim for f in top if f.confidence >= 0.5]
    if not claims: claims = [f.claim for f in top[:3]]
    if len(claims)==1: return f"Key finding on '{topic}': {claims[0]}"
    if len(claims)==2: return f"Research on '{topic}' reveals that {claims[0].lower().rstrip('.')}. Additionally, {claims[1].lower().rstrip('.')}."
    return f"Research on '{topic}' reveals that {claims[0].lower().rstrip('.')}. Furthermore, {claims[1].lower().rstrip('.')}. Notably, {claims[2].lower().rstrip('.')}."

def _build_source_summary(sources):
    if not sources: return {"total_sources":0,"reliability_distribution":{}}
    tiers = Counter(s.get("reliability_tier","unknown") for s in sources)
    domains = [s.get("domain","unknown") for s in sources]
    dates = [s.get("date","unknown") for s in sources if s.get("date")!="unknown"]
    avg = sum(s.get("reliability_score",0.30) for s in sources)/len(sources)
    return {"total_sources":len(sources),"unique_domains":list(set(domains)),"reliability_distribution":dict(tiers),"average_reliability_score":round(avg,2),"date_range":f"{min(dates)} - {max(dates)}" if dates else "unknown","source_urls":[s.get("url","") for s in sources]}

def _classify_image_type(url, alt, ctx):
    c = (url+" "+alt+" "+ctx).lower()
    if any(k in c for k in ["chart","graph","plot","diagram","figure"]): return "chart"
    if any(k in c for k in ["map","atlas","cartograph","geograph"]): return "map"
    if any(k in c for k in ["diagram","schematic","flowchart","blueprint"]): return "diagram"
    if any(k in c for k in ["infographic","info-graphic","data viz"]): return "infographic"
    return "photo"

def _extract_visual_leads(sources):
    leads = []; seen = set()
    for src in sources:
        for img in src.get("images",[]):
            u = img.get("url","")
            if not u or u in seen: continue
            seen.add(u)
            alt = img.get("alt",""); ctx = img.get("context","")
            leads.append(VisualLead(url=u,description=alt or ctx[:200] or "Image",source_url=src.get("url",""),image_type=_classify_image_type(u,alt,ctx),license_note="Verify license before use."))
    return leads

def research(topic, max_sources=DEFAULT_MAX_SOURCES, include_x=True, verbose=False):
    started = datetime.now(timezone.utc)
    output = ResearchOutput(topic=topic, researched_at=started.isoformat(),
        methodology_note="Automated pipeline: DuckDuckGo search, trafilatura extraction, heuristic fact extraction, cross-verification by Jaccard similarity >= 0.4, confidence based on source reliability and corroboration. Verify independently before production use.")
    session = _create_session()
    all_extracted = []; all_facts = []
    if verbose: print(f"[1/5] Searching: {topic}", file=sys.stderr)
    search_results = _web_search(topic, max_results=max_sources*2)
    if include_x and HAS_DDGS:
        if verbose: print("[2/5] Searching X/Twitter...", file=sys.stderr)
        search_results.extend(_search_x(topic))
    if not search_results:
        output.key_narrative = f"No results found for '{topic}'."
        return output
    seen = set(); unique = []
    for r in search_results:
        u = r.get("url","")
        if u and u not in seen: seen.add(u); unique.append(r)
    if verbose: print(f"[3/5] Extracting {min(max_sources,len(unique))} sources...", file=sys.stderr)
    for i, r in enumerate(unique[:max_sources]):
        url = r["url"]
        if verbose: print(f"  [{i+1}] {url}", file=sys.stderr)
        ex = _web_extract(url, session)
        if not ex: continue
        ex["search_title"] = r.get("title",""); ex["search_snippet"] = r.get("snippet","")
        sn = ex.get("title","") or r.get("title","")
        tier, score = _assess_source_reliability(url, sn)
        ex["reliability_tier"] = tier; ex["reliability_score"] = score
        all_extracted.append(ex)
        sf = _extract_facts(ex["text"], url, sn[:100], tier, score, ex.get("date","unknown"))
        if verbose and sf: print(f"    -> {len(sf)} facts", file=sys.stderr)
        all_facts.extend(sf)
        time.sleep(SEARCH_SLEEP)
    if verbose: print(f"[4/5] Cross-verifying {len(all_facts)} raw facts...", file=sys.stderr)
    merged = _cross_verify_facts(all_facts)
    if verbose: print(f"[5/5] Assembling output...", file=sys.stderr)
    merged.sort(key=lambda f: f.confidence, reverse=True)
    output.facts = merged
    output.source_summary = _build_source_summary(all_extracted)
    output.key_narrative = _generate_key_narrative(topic, merged)
    output.visual_leads = _extract_visual_leads(all_extracted)
    output.contested_flags = _identify_contested_flags(merged)
    elapsed = (datetime.now(timezone.utc)-started).total_seconds()
    if verbose: print(f"[DONE] {elapsed:.1f}s | {len(output.facts)} facts | {len(output.visual_leads)} visuals | {len(output.contested_flags)} flags", file=sys.stderr)
    return output

def main():
    parser = argparse.ArgumentParser(description="Researcher Agent - fact-checked web research")
    parser.add_argument("topic", nargs="?", help="Topic string")
    parser.add_argument("--topic","-t", dest="topic_flag", help="Topic (alt)")
    parser.add_argument("--max-sources","-n", type=int, default=DEFAULT_MAX_SOURCES, help=f"Max sources (default: {DEFAULT_MAX_SOURCES})")
    parser.add_argument("--output","-o", help="Output JSON file (default: stdout)")
    parser.add_argument("--no-x", action="store_true", help="Skip X/Twitter")
    parser.add_argument("--verbose","-v", action="store_true", help="Progress to stderr")
    parser.add_argument("--compact", action="store_true", help="Compact JSON")
    args = parser.parse_args()
    topic = args.topic or args.topic_flag
    if not topic: parser.error("No topic provided.")
    result = research(topic=topic, max_sources=args.max_sources, include_x=not args.no_x, verbose=args.verbose)
    js = result.to_json(indent=None if args.compact else 2)
    if args.output:
        with open(args.output, "w") as f: f.write(js)
        print(f"[INFO] Saved to {args.output}", file=sys.stderr)
    else:
        print(js)

if __name__ == "__main__":
    main()
