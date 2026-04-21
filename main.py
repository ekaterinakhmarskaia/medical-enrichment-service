from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Medical Enrichment Service",
    description="Enriches medical profile JSON with ICD-11 and RxNorm codes. Input: parsed Health Profile JSON from Cortex. Output: same JSON enriched with medical codes.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── credentials ───────────────────────────────────────────────────────────────
ICD11_CLIENT_ID     = "4df4a7e6-1f0b-473b-a17c-c9dc0ba21edf_ea4c313d-5e44-46b8-9bf4-6b7978407738"
ICD11_CLIENT_SECRET = "c9gfeNQT6CdDuL40NxXD5/md0OlNeka5uUEX5l5hTXw="

RXNORM_BASE      = "https://rxnav.nlm.nih.gov/REST"
ICD11_TOKEN_URL  = "https://icdaccessmanagement.who.int/connect/token"
ICD11_SEARCH_URL = "https://id.who.int/icd/release/11/2024-01/mms/search"
ICD11_ENTITY_URL = "https://id.who.int/icd/release/11/2024-01/mms"
ICD11_RELEASE    = "2024-01"

CLUSTER_FALLBACK = {
    "cardiovascular": {"icd11_code": "MB48.Z", "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/1890374210", "note": "Dizziness or giddiness, unspecified"},
    "fatigue":        {"icd11_code": "MG22",   "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/1109546957", "note": "Fatigue"},
    "cognitive":      {"icd11_code": "MB4D",   "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/1569881604", "note": "Cognitive symptoms"},
    "pain":           {"icd11_code": "MG30.0", "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/1581976053", "note": "Chronic primary pain"},
    "sleep":          {"icd11_code": "7A00",   "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/590211325",  "note": "Insomnia disorder"},
    "digestive":      {"icd11_code": "MD90.Z", "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/149932393",  "note": "Digestive symptoms, unspecified"},
    "mental":         {"icd11_code": "6B4Z",   "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/1407952941", "note": "Anxiety or fear-related disorder"},
    "hormonal":       {"icd11_code": "5A2Z",   "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/1048324574", "note": "Endocrine or metabolic condition"},
    "other":          {"icd11_code": "MG2Y",   "icd11_uri": "https://id.who.int/icd/release/11/2024-01/mms/438943684",  "note": "Other general symptoms"},
}

HRV_HR_CLASSES = {
    "Beta-Adrenergic Blocker","Antiarrhythmic","Cardiac Glycoside",
    "Selective Serotonin Reuptake Inhibitor","Serotonin and Norepinephrine Reuptake Inhibitor",
    "Tricyclic Antidepressant","Norepinephrine-Dopamine Reuptake Inhibitor",
    "Noradrenergic and Specific Serotonergic Antidepressant","Serotonin Modulator",
    "Atypical Antipsychotic","Typical Antipsychotic","Mood Stabilizer","Benzodiazepine",
    "Anxiolytic","Sedative Hypnotic","Melatonin Receptor Agonist","Orexin Receptor Antagonist",
    "Central Nervous System Stimulant","Norepinephrine Reuptake Inhibitor",
    "Thyroid Hormone","Antithyroid Agent","Estrogen","Progestin","Androgen",
    "Alpha-1 Adrenergic Agonist","Alpha-Adrenergic Agonist","Central Alpha-2 Adrenergic Agonist",
    "Alpha-1 Adrenergic Blocker","Calcium Channel Blocker",
    "Angiotensin-Converting Enzyme Inhibitor","Angiotensin II Receptor Blocker",
    "Serotonin-1 Receptor Agonist","Short-Acting Beta-2 Agonist","Long-Acting Beta-2 Agonist",
    "Dopamine Receptor Agonist","Dopamine Precursor","Monoamine Oxidase Inhibitor",
    "Acetylcholinesterase Inhibitor","Opioid Analgesic","Fluoroquinolone Antibacterial",
    "Macrolide Antibacterial","Phosphodiesterase-5 Inhibitor","Beta-3 Adrenergic Agonist",
    "Muscarinic Antagonist","Dopamine Antagonist","Serotonin-3 Receptor Antagonist",
    "Methylxanthine","Histamine-1 Receptor Antagonist","Somatostatin Analog",
}

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _http_get(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning("GET failed %s: %s", url, e)
    return None

def _http_post_form(url, data, timeout=10):
    body = urllib.parse.urlencode(data).encode()
    req  = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning("POST failed %s: %s", url, e)
    return None

def _extract_str(obj):
    if isinstance(obj, str): return obj
    if isinstance(obj, dict): return obj.get("@value")
    return None

# ── RxNorm ───────────────────────────────────────────────────────────────────
class RxNormClient:
    DELAY = 0.05
    def __init__(self): self._cache = {}

    def _get_rxcui(self, name):
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"name": name, "search": 2})
        data = _http_get(f"{RXNORM_BASE}/rxcui.json?{params}")
        if data:
            ids = data.get("idGroup", {}).get("rxnormId", [])
            if ids: return ids[0]
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"term": name, "maxEntries": 1})
        data = _http_get(f"{RXNORM_BASE}/approximateTerm.json?{params}")
        if data:
            candidates = data.get("approximateGroup", {}).get("candidate", [])
            if candidates: return candidates[0].get("rxcui")
        return None

    def _to_ingredient_rxcui(self, rxcui):
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"tty": "IN"})
        data = _http_get(f"{RXNORM_BASE}/rxcui/{rxcui}/related.json?{params}")
        if data:
            for g in data.get("relatedGroup", {}).get("conceptGroup", []):
                props = g.get("conceptProperties", [])
                if props: return props[0]["rxcui"]
        return rxcui

    def _get_epc(self, rxcui):
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"rxcui": rxcui, "relaSource": "FDASPL", "relas": "has_EPC"})
        data = _http_get(f"{RXNORM_BASE}/rxclass/class/byRxcui.json?{params}")
        if data:
            for item in data.get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", []):
                cls = item.get("rxclassMinConceptItem", {})
                if cls.get("classType") == "EPC": return cls.get("className")
        return None

    def resolve(self, name_generic):
        key = (name_generic or "").lower().strip()
        if not key: return {"rxnorm_id": None, "drug_class_epc": None, "affects_hrv_hr": False, "resolved": False}
        if key in self._cache: return self._cache[key]
        rxcui = self._get_rxcui(name_generic)
        if not rxcui:
            result = {"rxnorm_id": None, "drug_class_epc": None, "affects_hrv_hr": False, "resolved": False}
            self._cache[key] = result
            return result
        in_rxcui = self._to_ingredient_rxcui(rxcui)
        epc = self._get_epc(in_rxcui)
        affects = epc in HRV_HR_CLASSES if epc else False
        result = {"rxnorm_id": in_rxcui, "drug_class_epc": epc, "affects_hrv_hr": affects, "resolved": True}
        self._cache[key] = result
        return result

# ── ICD-11 ───────────────────────────────────────────────────────────────────
class ICD11Client:
    def __init__(self):
        self._token = None
        self._expires_at = 0.0
        self._cache = {}

    def _get_token(self):
        if self._token and time.time() < self._expires_at - 60: return self._token
        data = _http_post_form(ICD11_TOKEN_URL, {
            "client_id": ICD11_CLIENT_ID, "client_secret": ICD11_CLIENT_SECRET,
            "scope": "icdapi_access", "grant_type": "client_credentials",
        })
        if not data or "access_token" not in data: return None
        self._token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600)
        return self._token

    def _headers(self):
        token = self._get_token()
        if not token: return None
        return {"Authorization": f"Bearer {token}", "Accept": "application/json",
                "Accept-Language": "en", "API-Version": "v2"}

    def _search_one(self, query, flexisearch, headers):
        time.sleep(0.1)
        params = urllib.parse.urlencode({"q": query, "flatResults": "true",
            "highlightingEnabled": "false", "useFlexisearch": "true" if flexisearch else "false",
            "medicalCodingMode": "true"})
        data = _http_get(f"{ICD11_SEARCH_URL}?{params}", headers=headers, timeout=10)
        if not data: return None
        entities = data.get("destinationEntities", [])
        return entities[0] if entities else None

    def resolve(self, name_display, name_user=None, cluster=None, use_fallback=False):
        cache_key = (name_display or "").lower().strip()
        if cache_key in self._cache: return self._cache[cache_key]
        empty = {"icd11_code": None, "icd11_uri": None, "resolved": False}
        headers = self._headers()
        if not headers: self._cache[cache_key] = empty; return empty

        attempts = [(name_display, False), (name_display, True)]
        if name_user and name_user != name_display:
            attempts += [(name_user, False), (name_user, True)]

        for query, flex in attempts:
            if not query: continue
            result = self._search_one(query, flex, headers)
            if result:
                uri = result.get("id", "").replace("http://", "https://")
                code = result.get("theCode") or None
                r = {"icd11_code": code, "icd11_uri": uri or None, "resolved": bool(code)}
                self._cache[cache_key] = r
                return r

        if use_fallback and cluster and cluster in CLUSTER_FALLBACK:
            fb = CLUSTER_FALLBACK[cluster]
            r = {"icd11_code": fb["icd11_code"], "icd11_uri": fb["icd11_uri"], "resolved": True}
            self._cache[cache_key] = r
            return r

        self._cache[cache_key] = empty
        return empty

# ── Service ───────────────────────────────────────────────────────────────────
rxnorm = RxNormClient()
icd11  = ICD11Client()

def enrich_condition(c):
    c = dict(c)
    if c.get("icd11_code"): return c
    r = icd11.resolve(c.get("name_display") or c.get("name_user", ""), c.get("name_user"))
    c.update({"icd11_code": r["icd11_code"], "icd11_uri": r["icd11_uri"],
               "is_unresolved": not r["resolved"]})
    return c

def enrich_medication(m):
    m = dict(m)
    if m.get("rxnorm_id"): return m
    r = rxnorm.resolve(m.get("name_generic") or m.get("name_user", ""))
    m["rxnorm_id"] = r["rxnorm_id"]
    m["is_unresolved"] = not r["resolved"]
    if r["drug_class_epc"]:
        m["drug_class_epc"] = r["drug_class_epc"]
        m["affects_hrv_hr"] = r["affects_hrv_hr"]
    return m

def enrich_symptom(s):
    s = dict(s)
    if s.get("icd11_code"): return s
    r = icd11.resolve(s.get("name_display") or s.get("name_user", ""),
                      s.get("name_user"), s.get("cluster"), use_fallback=True)
    s.update({"icd11_code": r["icd11_code"], "icd11_uri": r["icd11_uri"],
               "is_unresolved": not r["resolved"]})
    return s

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "Medical Enrichment Service", "version": "1.0.0",
            "docs": "/docs", "usage": "POST /enrich with Health Profile JSON"}

@app.post("/enrich")
def enrich(payload: dict):
    """
    Enriches Health Profile JSON with ICD-11 and RxNorm codes.

    Input: JSON from Cortex (conditions, medications, symptoms arrays).
    Output: same structure with icd11_code, icd11_uri, rxnorm_id, drug_class_epc added.
    """
    try:
        log.info("Enriching: conditions=%d medications=%d symptoms=%d",
                 len(payload.get("conditions", [])),
                 len(payload.get("medications", [])),
                 len(payload.get("symptoms", [])))
        result = dict(payload)
        result["conditions"]  = [enrich_condition(c) for c in payload.get("conditions", [])]
        result["medications"] = [enrich_medication(m) for m in payload.get("medications", [])]
        result["symptoms"]    = [enrich_symptom(s)    for s in payload.get("symptoms", [])]
        result["_meta"] = {"icd11_release": ICD11_RELEASE, "enriched_at": time.time()}
        return result
    except Exception as e:
        log.error("Enrichment failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}
