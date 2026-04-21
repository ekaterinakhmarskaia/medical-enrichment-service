import os
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ── credentials from environment variables ────────────────────────────────────
ICD11_CLIENT_ID     = os.environ.get("ICD11_CLIENT_ID", "")
ICD11_CLIENT_SECRET = os.environ.get("ICD11_CLIENT_SECRET", "")

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── API constants ─────────────────────────────────────────────────────────────
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
    "Beta-Adrenergic Blocker", "Antiarrhythmic", "Cardiac Glycoside",
    "Selective Serotonin Reuptake Inhibitor",
    "Serotonin and Norepinephrine Reuptake Inhibitor",
    "Tricyclic Antidepressant", "Norepinephrine-Dopamine Reuptake Inhibitor",
    "Noradrenergic and Specific Serotonergic Antidepressant",
    "Serotonin Modulator", "Atypical Antipsychotic", "Typical Antipsychotic",
    "Mood Stabilizer", "Benzodiazepine", "Anxiolytic", "Sedative Hypnotic",
    "Melatonin Receptor Agonist", "Orexin Receptor Antagonist",
    "Central Nervous System Stimulant", "Norepinephrine Reuptake Inhibitor",
    "Histamine-3 Receptor Antagonist", "Thyroid Hormone", "Antithyroid Agent",
    "Estrogen", "Progestin", "Androgen",
    "Alpha-1 Adrenergic Agonist", "Alpha-Adrenergic Agonist",
    "Central Alpha-2 Adrenergic Agonist", "Alpha-1 Adrenergic Blocker",
    "Calcium Channel Blocker",
    "Angiotensin-Converting Enzyme Inhibitor",
    "Angiotensin II Receptor Blocker",
    "Hyperpolarization-Activated Cyclic Nucleotide-Gated Channel Blocker",
    "Serotonin-1 Receptor Agonist",
    "Short-Acting Beta-2 Agonist", "Long-Acting Beta-2 Agonist",
    "Dopamine Receptor Agonist", "Dopamine Precursor",
    "Monoamine Oxidase Inhibitor", "Acetylcholinesterase Inhibitor",
    "Opioid Analgesic", "Fluoroquinolone Antibacterial",
    "Macrolide Antibacterial", "Phosphodiesterase-5 Inhibitor",
    "Beta-3 Adrenergic Agonist", "Muscarinic Antagonist",
    "Dopamine Antagonist", "Serotonin-3 Receptor Antagonist",
    "Methylxanthine", "Histamine-1 Receptor Antagonist", "Somatostatin Analog",
}

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _http_get(url: str, headers: dict = None, timeout: int = 8) -> Optional[dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("HTTP %s: %s", e.code, url)
    except Exception as e:
        log.warning("Request failed: %s", e)
    return None

def _http_post_form(url: str, data: dict, timeout: int = 10) -> Optional[dict]:
    body = urllib.parse.urlencode(data).encode()
    req  = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning("POST failed: %s", e)
    return None

def _extract_str(obj) -> Optional[str]:
    if isinstance(obj, str):   return obj
    if isinstance(obj, dict):  return obj.get("@value")
    return None

# ── RxNorm client ─────────────────────────────────────────────────────────────
class RxNormClient:
    DELAY = 0.05

    def __init__(self):
        self._cache: dict = {}

    def _get_rxcui(self, name: str) -> Optional[str]:
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"name": name, "search": 2})
        data   = _http_get(f"{RXNORM_BASE}/rxcui.json?{params}")
        if data:
            ids = data.get("idGroup", {}).get("rxnormId", [])
            if ids: return ids[0]
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"term": name, "maxEntries": 1})
        data   = _http_get(f"{RXNORM_BASE}/approximateTerm.json?{params}")
        if data:
            candidates = data.get("approximateGroup", {}).get("candidate", [])
            if candidates: return candidates[0].get("rxcui")
        return None

    def _to_ingredient_rxcui(self, rxcui: str) -> str:
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"tty": "IN"})
        data   = _http_get(f"{RXNORM_BASE}/rxcui/{rxcui}/related.json?{params}")
        if data:
            for g in data.get("relatedGroup", {}).get("conceptGroup", []):
                props = g.get("conceptProperties", [])
                if props: return props[0]["rxcui"]
        return rxcui

    def _get_property(self, rxcui: str, prop_name: str) -> Optional[str]:
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"propName": prop_name})
        data   = _http_get(f"{RXNORM_BASE}/rxcui/{rxcui}/property.json?{params}")
        if data:
            props = data.get("propConceptGroup", {}).get("propConcept", [])
            for p in props:
                if p.get("propName") == prop_name: return p.get("propValue")
            if props: return props[0].get("propValue")
        return None

    def _get_all_codes(self, rxcui: str) -> dict:
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"prop": "codes"})
        data   = _http_get(f"{RXNORM_BASE}/rxcui/{rxcui}/allProperties.json?{params}")
        result = {}
        if data:
            for p in data.get("propConceptGroup", {}).get("propConcept", []):
                name = p.get("propName", "")
                val  = p.get("propValue")
                if name and val:
                    if name not in result:              result[name] = val
                    elif isinstance(result[name], list): result[name].append(val)
                    else:                               result[name] = [result[name], val]
        return result

    def _get_all_attributes(self, rxcui: str) -> dict:
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"prop": "attributes"})
        data   = _http_get(f"{RXNORM_BASE}/rxcui/{rxcui}/allProperties.json?{params}")
        result = {}
        if data:
            for p in data.get("propConceptGroup", {}).get("propConcept", []):
                name = p.get("propName", "")
                val  = p.get("propValue")
                if name and val: result[name] = val
        return result

    def _get_epc(self, rxcui: str) -> Optional[str]:
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"rxcui": rxcui, "relaSource": "FDASPL", "relas": "has_EPC"})
        data   = _http_get(f"{RXNORM_BASE}/rxclass/class/byRxcui.json?{params}")
        if data:
            for item in data.get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", []):
                cls = item.get("rxclassMinConceptItem", {})
                if cls.get("classType") == "EPC": return cls.get("className")
        return None

    def _get_brand_names(self, rxcui: str) -> list:
        time.sleep(self.DELAY)
        params = urllib.parse.urlencode({"tty": "BN"})
        data   = _http_get(f"{RXNORM_BASE}/rxcui/{rxcui}/related.json?{params}")
        brands = []
        if data:
            for g in data.get("relatedGroup", {}).get("conceptGroup", []):
                for c in g.get("conceptProperties", []):
                    name = c.get("name")
                    if name and name not in brands: brands.append(name)
        return brands[:10]

    def resolve(self, name_generic: str) -> dict:
        key = (name_generic or "").lower().strip()
        if not key:
            return {"rxnorm_id": None, "rxnorm_name": None, "drug_class_epc": None,
                    "atc_code": None, "snomed_code": None, "drugbank_id": None,
                    "schedule": None, "available_strengths": None, "brand_names": [],
                    "affects_hrv_hr": False, "resolved": False}
        if key in self._cache: return self._cache[key]

        log.info("RxNorm <- '%s'", name_generic)
        rxcui = self._get_rxcui(name_generic)
        if not rxcui:
            result = {"rxnorm_id": None, "rxnorm_name": None, "drug_class_epc": None,
                      "atc_code": None, "snomed_code": None, "drugbank_id": None,
                      "schedule": None, "available_strengths": None, "brand_names": [],
                      "affects_hrv_hr": False, "resolved": False}
            self._cache[key] = result
            return result

        in_rxcui = self._to_ingredient_rxcui(rxcui)
        codes    = self._get_all_codes(in_rxcui)
        atc         = codes.get("ATC")
        snomed_code = codes.get("SNOMEDCT")
        drugbank_id = codes.get("DRUGBANK")
        rxnorm_name = codes.get("RxNorm Name") or self._get_property(in_rxcui, "RxNorm Name")
        attrs       = self._get_all_attributes(in_rxcui)
        epc         = self._get_epc(in_rxcui)
        brand_names = self._get_brand_names(in_rxcui)
        affects     = epc in HRV_HR_CLASSES if epc else False

        log.info("  rxcui=%s  epc=%s  atc=%s  brands=%d", in_rxcui, epc, atc, len(brand_names))

        result = {
            "rxnorm_id":           in_rxcui,
            "rxnorm_name":         rxnorm_name,
            "drug_class_epc":      epc,
            "atc_code":            atc,
            "snomed_code":         snomed_code if isinstance(snomed_code, str) else (snomed_code[0] if isinstance(snomed_code, list) else None),
            "drugbank_id":         drugbank_id,
            "schedule":            attrs.get("SCHEDULE"),
            "available_strengths": attrs.get("AVAILABLE_STRENGTH"),
            "brand_names":         brand_names,
            "affects_hrv_hr":      affects,
            "resolved":            True,
        }
        self._cache[key] = result
        return result

# ── ICD-11 client ─────────────────────────────────────────────────────────────
class ICD11Client:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id     = client_id
        self.client_secret = client_secret
        self._token:      Optional[str] = None
        self._expires_at: float         = 0.0
        self._cache: dict               = {}

    def _available(self) -> bool:
        return bool(self.client_id and self.client_secret
                    and self.client_id != "YOUR_CLIENT_ID")

    def _get_token(self) -> Optional[str]:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        data = _http_post_form(ICD11_TOKEN_URL, {
            "client_id": self.client_id, "client_secret": self.client_secret,
            "scope": "icdapi_access", "grant_type": "client_credentials",
        })
        if not data or "access_token" not in data:
            log.warning("ICD-11: failed to obtain token")
            return None
        self._token      = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600)
        log.info("ICD-11: token obtained")
        return self._token

    def _headers(self) -> Optional[dict]:
        token = self._get_token()
        if not token: return None
        return {"Authorization": f"Bearer {token}", "Accept": "application/json",
                "Accept-Language": "en", "API-Version": "v2"}

    def _score_entity(self, entity: dict, query: str) -> int:
        """
        Score an ICD-11 result by title match quality. Higher = better.

          +100  exact title match
          +60   title starts with query
          +40   title contains all query words
          +10   bonus: query appears as a phrase inside the title
          +20   title contains any query word
          -30   catch-all qualifiers: secondary, unspecified, other specified, NOS
          -50   etiological qualifiers: induced by, due to, caused by, substance,
                associated with — these are secondary/substance conditions,
                almost never what a user typing plain symptoms means
        """
        title = (entity.get("title") or "").lower().strip()
        q     = query.lower().strip()
        score = 0

        if title == q:
            score += 100
        elif title.startswith(q):
            score += 60
        else:
            words = [w for w in q.split() if len(w) > 2]
            if words:
                if all(w in title for w in words):
                    score += 40
                    if q in title:       # phrase bonus
                        score += 10
                elif any(w in title for w in words):
                    score += 20

        # Penalise catch-all / unspecified
        for bad in ("secondary", "other specified", "unspecified", " nos ",
                    "not elsewhere classified"):
            if bad in title:
                score -= 30

        # Strongly penalise substance-induced / etiological secondary conditions
        for bad in ("induced by", "due to", "caused by", "associated with",
                    "related to", "in the context of", "substance",
                    "multiple specified", "single specified", "psychoactive"):
            if bad in title:
                score -= 50

        return score

    def _search_best(self, query: str, flexisearch: bool, headers: dict) -> Optional[dict]:
        """Fetch up to 10 results and return the one with the highest score."""
        time.sleep(0.1)
        params = urllib.parse.urlencode({
            "q": query, "flatResults": "true", "highlightingEnabled": "false",
            "useFlexisearch": "true" if flexisearch else "false",
            "medicalCodingMode": "true",
        })
        data = _http_get(f"{ICD11_SEARCH_URL}?{params}", headers=headers, timeout=10)
        if not data: return None
        entities = data.get("destinationEntities", [])
        if not entities: return None

        # Score all results and pick the best
        scored = [(self._score_entity(e, query), i, e) for i, e in enumerate(entities)]
        scored.sort(key=lambda x: (-x[0], x[1]))  # highest score first, stable by position
        best_score, _, best = scored[0]

        log.info("  scored %d results, best score=%d title='%s'",
                 len(entities), best_score, (best.get("title") or "")[:50])
        return best

    def _get_entity_details(self, entity_uri: str, headers: dict) -> dict:
        time.sleep(0.1)
        url = entity_uri.replace("http://", "https://")
        if "/icd/entity/" in url:
            entity_id = url.split("/icd/entity/")[-1].rstrip("/")
            url = f"{ICD11_ENTITY_URL}/{entity_id}"

        data = _http_get(url, headers=headers, timeout=10)
        if not data: return {}
        result = {}

        defn = data.get("definition")
        if defn: result["icd11_definition"] = _extract_str(defn)

        synonyms = []
        for s in data.get("synonym", []):
            label = s.get("label") if isinstance(s, dict) else s
            text  = _extract_str(label) if isinstance(label, dict) else _extract_str(s)
            if text: synonyms.append(text)
        if synonyms: result["icd11_synonyms"] = synonyms

        inclusions = []
        for inc in data.get("inclusion", []):
            label = inc.get("label") if isinstance(inc, dict) else inc
            text  = _extract_str(label) if isinstance(label, dict) else _extract_str(inc)
            if text: inclusions.append(text)
        if inclusions: result["icd11_inclusions"] = inclusions

        exclusions = []
        for exc in data.get("exclusion", []):
            label   = exc.get("label") if isinstance(exc, dict) else exc
            text    = _extract_str(label) if isinstance(label, dict) else _extract_str(exc)
            lin_ref = exc.get("linearizationReference") if isinstance(exc, dict) else None
            entry   = {"label": text} if text else {}
            if lin_ref: entry["reference"] = lin_ref
            if entry: exclusions.append(entry)
        if exclusions: result["icd11_exclusions"] = exclusions

        browser_url = data.get("browserUrl")
        if browser_url: result["icd11_browser_url"] = browser_url

        index_terms = []
        for it in data.get("indexTerm", []):
            label = it.get("label") if isinstance(it, dict) else it
            text  = _extract_str(label) if isinstance(label, dict) else _extract_str(it)
            if text and text not in index_terms: index_terms.append(text)
        if index_terms: result["icd11_index_terms"] = index_terms[:20]

        return result

    def resolve(self, name_display: str, name_user: str = None,
                cluster: str = None, use_fallback: bool = False,
                fetch_details: bool = True) -> dict:
        cache_key = (name_display or "").lower().strip()
        if cache_key in self._cache: return self._cache[cache_key]

        empty = {"icd11_code": None, "icd11_uri": None, "icd11_name": None,
                 "icd11_definition": None, "icd11_synonyms": None,
                 "icd11_inclusions": None, "icd11_exclusions": None,
                 "icd11_browser_url": None, "icd11_index_terms": None,
                 "name_display": name_display, "resolved": False, "resolution": "not_found"}

        if not self._available():
            empty["resolution"] = "no_credentials"
            self._cache[cache_key] = empty
            return empty

        headers = self._headers()
        if not headers:
            empty["resolution"] = "token_error"
            self._cache[cache_key] = empty
            return empty

        attempts = [
            (name_display, False, "exact/name_display"),
            (name_display, True,  "flex/name_display"),
        ]
        if name_user and name_user != name_display:
            attempts += [
                (name_user, False, "exact/name_user"),
                (name_user, True,  "flex/name_user"),
            ]

        best = None
        for query, flex, label in attempts:
            if not query: continue
            log.info("ICD-11 <- '%s' [%s]", query, label)
            found = self._search_best(query, flex, headers)
            if found:
                best = found
                log.info("  [found] code=%s title='%s'",
                         found.get("theCode"), (found.get("title") or "")[:50])
                break
            else:
                log.info("  [empty]")

        if best:
            uri  = best.get("id", "").replace("http://", "https://")
            code = best.get("theCode") or None
            result = {
                "icd11_code": code, "icd11_uri": uri or None,
                "icd11_name": best.get("title"),
                "icd11_definition": None, "icd11_synonyms": None,
                "icd11_inclusions": None, "icd11_exclusions": None,
                "icd11_browser_url": None, "icd11_index_terms": None,
                "name_display": name_display, "resolved": bool(code), "resolution": "api_search",
            }
            if fetch_details and uri:
                result.update(self._get_entity_details(uri, headers))
            self._cache[cache_key] = result
            return result

        if use_fallback and cluster and cluster in CLUSTER_FALLBACK:
            fb = CLUSTER_FALLBACK[cluster]
            result = {
                "icd11_code": fb["icd11_code"], "icd11_uri": fb["icd11_uri"],
                "icd11_name": fb["note"],
                "icd11_definition": None, "icd11_synonyms": None,
                "icd11_inclusions": None, "icd11_exclusions": None,
                "icd11_browser_url": None, "icd11_index_terms": None,
                "name_display": name_display, "resolved": True,
                "resolution": f"cluster_fallback/{cluster}",
            }
            if fetch_details:
                result.update(self._get_entity_details(fb["icd11_uri"], headers))
            self._cache[cache_key] = result
            return result

        self._cache[cache_key] = empty
        return empty

# ── Enrichment service ────────────────────────────────────────────────────────
ICD11_FIELDS = [
    "icd11_code", "icd11_uri", "icd11_name", "icd11_definition",
    "icd11_synonyms", "icd11_inclusions", "icd11_exclusions",
    "icd11_browser_url", "icd11_index_terms", "is_unresolved", "_resolution",
]

class MedicalEnrichmentService:
    def __init__(self):
        self.rxnorm = RxNormClient()
        self.icd11  = ICD11Client(
            client_id     = ICD11_CLIENT_ID,
            client_secret = ICD11_CLIENT_SECRET,
        )

    def _enrich_condition(self, condition: dict) -> dict:
        c = dict(condition)
        if c.get("icd11_code") and c.get("icd11_uri"): return c
        r = self.icd11.resolve(
            name_display  = c.get("name_display") or c.get("name_user", ""),
            name_user     = c.get("name_user"),
            use_fallback  = False,
            fetch_details = True,
        )
        for field in ICD11_FIELDS:
            if field in r: c[field] = r[field]
        c["is_unresolved"] = not r["resolved"]
        c["_resolution"]   = r.get("resolution")
        return c

    def _enrich_medication(self, medication: dict) -> dict:
        m = dict(medication)
        if m.get("rxnorm_id"): return m
        r = self.rxnorm.resolve(m.get("name_generic") or m.get("name_user", ""))
        m["rxnorm_id"]           = r["rxnorm_id"]
        m["rxnorm_name"]         = r.get("rxnorm_name")
        m["atc_code"]            = r.get("atc_code")
        m["snomed_code"]         = r.get("snomed_code")
        m["drugbank_id"]         = r.get("drugbank_id")
        m["schedule"]            = r.get("schedule")
        m["available_strengths"] = r.get("available_strengths")
        m["brand_names"]         = r.get("brand_names", [])
        m["is_unresolved"]       = not r["resolved"]
        if r["drug_class_epc"]:
            llm_epc = m.get("drug_class_epc")
            if llm_epc and llm_epc != r["drug_class_epc"]:
                log.info("  EPC conflict '%s': llm=%s -> api=%s",
                         m.get("name_generic"), llm_epc, r["drug_class_epc"])
            m["drug_class_epc"] = r["drug_class_epc"]
            m["affects_hrv_hr"] = r["affects_hrv_hr"]
        return m

    def _enrich_symptom(self, symptom: dict) -> dict:
        s = dict(symptom)
        if s.get("icd11_code") and s.get("icd11_uri"): return s
        r = self.icd11.resolve(
            name_display  = s.get("name_display") or s.get("name_user", ""),
            name_user     = s.get("name_user"),
            cluster       = s.get("cluster"),
            use_fallback  = True,
            fetch_details = True,
        )
        for field in ICD11_FIELDS:
            if field in r: s[field] = r[field]
        s["is_unresolved"] = not r["resolved"]
        s["_resolution"]   = r.get("resolution")
        return s

    def enrich(self, payload: dict) -> dict:
        log.info("=" * 55)
        log.info("Enrichment: conditions=%d  medications=%d  symptoms=%d",
                 len(payload.get("conditions", [])),
                 len(payload.get("medications", [])),
                 len(payload.get("symptoms", [])))
        enriched = dict(payload)
        enriched["conditions"]  = [self._enrich_condition(c) for c in payload.get("conditions", [])]
        enriched["medications"] = [self._enrich_medication(m) for m in payload.get("medications", [])]
        enriched["symptoms"]    = [self._enrich_symptom(s)    for s in payload.get("symptoms", [])]
        enriched["_enrichment_meta"] = {
            "icd11_release":   ICD11_RELEASE,
            "icd11_available": self.icd11._available(),
        }
        log.info("Enrichment complete")
        log.info("=" * 55)
        return enriched

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Medical Enrichment Service",
    description=(
        "Enriches Health Profile JSON with ICD-11 and RxNorm codes.\n\n"
        "**Input**: parsed JSON from Cortex (conditions, medications, symptoms).\n\n"
        "**Output**: same JSON with `icd11_code`, `icd11_uri`, `icd11_definition`, "
        "`rxnorm_id`, `drug_class_epc`, `brand_names` and more added."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single shared instance — caches RxNorm and ICD-11 lookups in memory
service = MedicalEnrichmentService()

@app.get("/")
def root():
    return {
        "service": "Medical Enrichment Service",
        "version": "2.0.0",
        "docs": "/docs",
        "icd11_available": service.icd11._available(),
        "usage": "POST /enrich  with Health Profile JSON body",
    }

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/enrich")
def enrich(payload: dict):
    """
    Enriches Health Profile JSON.

    Send the raw JSON object from Cortex as the request body.
    Returns the same structure with medical codes added to each entity.
    """
    try:
        return service.enrich(payload)
    except Exception as e:
        log.error("Enrichment failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
