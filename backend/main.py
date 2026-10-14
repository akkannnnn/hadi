"""
Akkan AI - FastAPI Backend (v3.2)
Düzeltmeler:
- Karma (karma_yap) işleminde kilitli derslerin korunması ve doğru şekilde yeniden yazılması
- Kilitli derslerin ders yükünden düşülmesi sırasında öğretmen bilgisinin de kullanılması
- Proje kaydetme/yükleme işlemlerine kilitli slotların eklenmesi
- Ders yükü güncellemelerinde havuz senkronizasyonu iyileştirmeleri
- Karma işlemi sonrası kilitli derslerin eksiksiz geri getirilmesi
- Kopyala-yapıştır ve manuel ekleme sonrası havuzda (kalan) biriken mükerrer/hayalet derslerin senkronizasyon temizliği
"""

import copy
import io
import os
import json
import pickle
import random
import threading
import time
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.engine import program_hesapla, ORTOOLS_VAR
import backend.engine as engine_mod
from backend.state import state, _sutun_bul, _dogal_sirala
from backend.excel import excel_uret

# ══════════════════
#  PYDANTIC MODELLERİ
# ══════════════════
# --- DİNAMİK GÜN VE SAAT YARDIMCILARI ---
def get_max_gun():
    z = state.get("zaman_ayarlari") or {}
    return len(z.get("gunler") or ["1","2","3","4","5"])

def get_max_saat():
    z = state.get("zaman_ayarlari") or {}
    return int(z.get("ders_sayisi") or 8)
# ----------------------------------------
class ZamanAyarlari(BaseModel):
    baslama:           str  = "09:15"
    ders_dk:           int  = 40
    teneffus_dk:       int  = 10
    ogle_sonrasi_ders: int  = 5
    ogle_dk:           int  = 50
    ozel_saatler:      Dict[str, str] = {}
    gunler:            Optional[List[str]] = None
    ders_sayisi:       Optional[int]       = None
    gun_saatleri:      Optional[Dict[str, Any]] = None

class GelismisAyarlar(BaseModel):
    blok_stratejisi: str = "ikili"
    gun_yayilimi: bool = True
    ardisiklik_boslugu: int = 1
    ogr_snf_gunluk_limit: bool = True
    gunluk_max_blok: bool = True
    yogun_ders_egilimi: str = "yok"
    yogun_ders_saat_esigi: int = 5
    ders_iliskileri: List[Dict[str, str]] = []

class KisitlamaGuncelle(BaseModel):
    zaman_ayarlari:    ZamanAyarlari
    ogretmen_izinleri: Dict[str, List[List[int]]] = {}
    sinif_izinleri:    Dict[str, List[List[int]]] = {}
    gelismis_ayarlar:  Optional[GelismisAyarlar] = None

class KilitliKaydet(BaseModel):
    kilitler: Dict[str, Any] = {}

class DersYukuSatir(BaseModel):
    idx:           Optional[int] = None
    sinif:         str
    ders_adi:      str
    ogretmen:      str
    haftalik_saat: int

class SablonKopyala(BaseModel):
    kaynak_sinif: str
    hedef_sinif:  str

class DersPanoYapistir(BaseModel):
    pano:        List[Dict[str, Any]]
    hedef_sinif: str

class TakasIstek(BaseModel):
    sinif: str
    gun1:  int
    saat1: int
    gun2:  int
    saat2: int
    onay:  Optional[bool] = False

class HavuzYerlestir(BaseModel):
    kalan_idx: int
    gun:       int
    saat:      int

class TabloHavuzaAt(BaseModel):
    sinif: str
    gun:   int
    saat:  int

class ExcelIstek(BaseModel):
    siyah_beyaz: bool = False
class ExcelAyarlari(BaseModel):
    tekli_ders_f_size: int = 11
    tekli_ogr_f_size: int = 10
    tekli_snf_f_size: int = 11
    dortlu_ders_f_size: int = 8
    dortlu_ogr_f_size: int = 8
    dortlu_snf_f_size: int = 8
    tekli_row_h: int = 65
    dortlu_row_h: int = 30
    tekli_sutun_genislik: int = 26
    dortlu_sutun_genislik: int = 12
    gun_f_size: int = 9
    saat_f_size: int = 7
    gun_row_h: int = 15
    saat_col_w: int = 12

class AdIstek(BaseModel):
    ad: str
    secenek: Optional[str] = None

class ProgramOlusturIstek(BaseModel):
    sure_saniye: Optional[int] = 600

class GrupHavuzaAt(BaseModel):
    siniflar: List[str]
    gun: int
    saat: int

class TakasFarkliSinif(BaseModel):
    sinif1: str
    gun1: int
    saat1: int
    sinif2: str
    gun2: int
    saat2: int
    onay: Optional[bool] = False 

class ProgramDersGuncelleIstek(BaseModel):
    gun: int
    saat: int
    sinif: str
    ders: Optional[str] = None
    ogretmen: Optional[str] = None
    eski_ders: Optional[str] = None
    eski_ogretmen: Optional[str] = None
    yeni_ders: Optional[str] = None
    yeni_ogretmen: Optional[str] = None

class SlotYazItem(BaseModel):
    sinif: str
    gun: int
    saat: int
    ders: Optional[str] = None
    ogretmen: Optional[str] = None

class SlotYazIstek(BaseModel):
    yazimlar: List[SlotYazItem] = []
    silimler: List[SlotYazItem] = []

class KarmaIstek(BaseModel):
    siniflar:    List[str]
    sure_saniye: Optional[int] = 120

class OgretmenGuncelle(BaseModel):
    eski_ogretmen: str
    yeni_ogretmen: str
    ders_adi:      Optional[str] = None

# ════════════════════
#  YARDIMCI FONKSİYONLAR
# ════════════════════

def df_to_records(df) -> List[Dict]:
    if df is None or df.empty:
        return []
    df2 = df.reset_index().rename(columns={"index": "idx"})
    return df2.fillna("").to_dict(orient="records")

def izin_listesine_cevir(raw: Dict[str, List[List[int]]]) -> Dict[str, List]:
    return {k: [tuple(t) for t in v] for k, v in raw.items()}

def prog_normalize(prog: dict) -> dict:
    """Programdaki gün/saat anahtarlarını integer'a çevirir. Her zaman yeni bir dict döner."""
    if not prog:
        return {}
    prog = copy.deepcopy(prog)
    ilk = next(iter(prog))
    if isinstance(ilk, str):
        yeni = {}
        for g_key, saatler in prog.items():
            g = int(g_key)
            yeni[g] = {}
            for s_key, siniflar in saatler.items():
                yeni[g][int(s_key)] = siniflar
        return yeni
    return prog

def _kaydet_ve_yedekle():
    """Kilitleri ve kısıtlamaları dosyaya kaydeder."""
    try:
        with open("kilitler.json", "w", encoding="utf-8") as f:
            json.dump(state.get("kilitli_slotlar") or {}, f, ensure_ascii=False, indent=4)
        kz = {
            "zaman_ayarlari": state.get("zaman_ayarlari") or {},
            "ogretmen_izinleri": {k: [list(t) for t in v] for k, v in state.get("ogretmen_izinleri", {}).items()},
            "sinif_izinleri": {k: [list(t) for t in v] for k, v in state.get("sinif_izinleri", {}).items()},
            "gelismis_ayarlar": state.get("gelismis_ayarlar") or {}
        }
        with open("kisitlamalar.json", "w", encoding="utf-8") as f:
            json.dump(kz, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ Dosya kaydetme hatası: {e}")

# ══════════════════
#  FASTAPI UYGULAMASI
# ══════════════════

app = FastAPI(title="Akkan AI", version="3.2")

if os.path.exists("kilitler.json"):
    try:
        with open("kilitler.json", "r", encoding="utf-8") as f:
            state.set("kilitli_slotlar", json.load(f))
            print("✅ Kilitli dersler dosyadan başarıyla yüklendi.")
    except Exception as e:
        print(f"❌ Kilitler yüklenirken hata: {e}")

if os.path.exists("kisitlamalar.json"):
    try:
        with open("kisitlamalar.json", "r", encoding="utf-8") as f:
            kz = json.load(f)
            state.set("zaman_ayarlari", kz.get("zaman_ayarlari", {}))
            state.set("sinif_izinleri",    izin_listesine_cevir(kz.get("sinif_izinleri", {})))
            state.set("gelismis_ayarlar", kz.get("gelismis_ayarlar", state.get("gelismis_ayarlar")))
            print("✅ Kapalı saatler (kısıtlamalar) dosyadan başarıyla yüklendi.")
    except Exception as e:
        print(f"❌ Kısıtlamalar yüklenirken hata oluştu: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

@app.get("/api/durum")
def durum():
    state.saatleri_hesapla()
    s = state.snapshot()
    kilitli_ham = state.get("kilitli_slotlar") or {}
    df = s["df_ders_yuku"]

    ogr_saat = {}
    snf_saat = {}
    if df is not None and not df.empty:
        cols = list(df.columns)
        c_snf = _sutun_bul(cols, ["sinif", "class"])
        c_ogr = _sutun_bul(cols, ["ogretmen", "teacher"])
        c_sat = _sutun_bul(cols, ["saat", "hour"])
        for _, row in df.iterrows():
            try:
                saat = int(float(str(row[c_sat])))
                for x in str(row[c_ogr]).split("+"):
                    x = x.strip()
                    if x and x != "nan":
                        ogr_saat[x] = ogr_saat.get(x, 0) + saat
                for x in str(row[c_snf]).split("+"):
                    x = x.strip()
                    if x and x != "nan":
                        snf_saat[x] = snf_saat.get(x, 0) + saat
            except Exception:
                pass

    return {
        "ogretmen_listesi":      s["ogretmen_listesi"],
        "sinif_listesi":         s["sinif_listesi"],
        "ders_listesi":          s["ders_listesi"],
        "ogretmen_izinleri":     {k: [list(t) for t in v] for k, v in s["ogretmen_izinleri"].items()},
        "sinif_izinleri":        {k: [list(t) for t in v] for k, v in s["sinif_izinleri"].items()},
        "zaman_ayarlari":        s["zaman_ayarlari"],
        "aktif_saat_etiketleri": s["aktif_saat_etiketleri"],
        "df_yuklu":              df is not None,
        "satir_sayisi":          len(df) if df is not None else 0,
        "program_var":           s["uretilen_program"] is not None,
        "motor_calisiyor":       s["motor_calisiyor"],
        "ortools_var":           ORTOOLS_VAR,
        "ogretmen_saat_map":     ogr_saat,
        "sinif_saat_map":        snf_saat,
    }

@app.post("/api/excel-yukle")
async def excel_yukle(dosya: UploadFile = File(...)):
    icerik = await dosya.read()
    try:
        df = pd.read_excel(io.BytesIO(icerik))

        df.columns = [str(col).strip().lower() for col in df.columns]
        mapping = {
            'sinif': 'Sınıf', 'sınıf': 'Sınıf',
            'ders adı': 'Ders Adı', 'ders adi': 'Ders Adı', 'ders': 'Ders Adı',
            'ogretmen': 'Öğretmen', 'öğretmen': 'Öğretmen',
            'haftalık saat': 'Haftalık Saat', 'haftalik saat': 'Haftalık Saat', 'saat': 'Haftalık Saat'
        }
        df = df.rename(columns=mapping)

        beklenen = ['Sınıf', 'Ders Adı', 'Öğretmen', 'Haftalık Saat']
        eksik = [b for b in beklenen if b not in df.columns]
        if eksik:
            raise HTTPException(400, f"Eksik sütunlar: {eksik}. Lütfen Excel başlıklarını kontrol edin.")

        state.set("df_ders_yuku", df)
        state.set("ogretmen_izinleri", {})
        state.set("sinif_izinleri",    {})
        state.set("kilitli_slotlar",   {})
        state.set("uretilen_program",  None)
        state.set("uretilen_kalan",    [])

        # Fiziksel json dosyalarını temizle
        for dosya_adi in ["kilitler.json", "kisitlamalar.json"]:
            if os.path.exists(dosya_adi):
                try:
                    os.remove(dosya_adi)
                except Exception:
                    pass

        state.listeleri_guncelle()
        state.saatleri_hesapla()

        s = state.snapshot()
        return {
            "mesaj":           f"{dosya.filename} başarıyla yüklendi.",
            "ogretmen_sayisi": len(s["ogretmen_listesi"]),
            "sinif_sayisi":    len(s["sinif_listesi"]),
            "satir_sayisi":    len(s["df_ders_yuku"]) if s["df_ders_yuku"] is not None else 0,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Dosya okuma hatası: {e}")

@app.get("/api/ders-yuku")
def ders_yuku_listele():
    return {"satirlar": df_to_records(state.get("df_ders_yuku"))}

def _to_lower_tr(s):
    """Türkçe karakterleri ve büyük/küçük harfleri kusursuz eşitler."""
    return str(s).strip().replace('I', 'ı').replace('İ', 'i').lower()

def _norm_ogr(ogr_str):
    """Öğretmen parçalarını SIRALI ve küçük harfe normalize eder."""
    return " + ".join(sorted([_to_lower_tr(x) for x in str(ogr_str).split("+") if x.strip()]))

def _prog_satir_guncelle(eski_snf, yeni_snf, eski_ders, yeni_ders, eski_ogr, yeni_ogr):
    """Sadece spesifik bir satırdaki değişimi programa yansıtır, harf hatalarını affeder."""
    prog = prog_normalize(state.get("uretilen_program"))
    kalan = state.get("uretilen_kalan") or []
    
    if not prog: 
        prog = {}
    
    e_snf_list = [_to_lower_tr(x) for x in str(eski_snf).split("+") if x.strip()]
    y_snf_list = [_to_lower_tr(x) for x in str(yeni_snf).split("+") if x.strip()]
    sinif_degisti = (set(e_snf_list) != set(y_snf_list))
    
    e_ders_low = _to_lower_tr(eski_ders)
    y_ders_orj = str(yeni_ders).strip()
    e_ogr_low = _norm_ogr(eski_ogr)
    y_ogr_orj = str(yeni_ogr).strip()
    
    eski_parcalar = [x.strip() for x in str(eski_snf).split("+") if x.strip()]
    yeni_parcalar = [x.strip() for x in str(yeni_snf).split("+") if x.strip()]
    
    for g in range(get_max_gun()):
        for s in range(1, get_max_saat() + 1):
            if s not in prog.get(g, {}): continue
            
            eslesme = True
            for p in eski_parcalar:
                matched_key = None
                for k_prog in prog[g][s].keys():
                    if _to_lower_tr(k_prog) == _to_lower_tr(p):
                        matched_key = k_prog
                        break
                
                h = prog[g][s].get(matched_key) if matched_key else None
                
                if not h or _to_lower_tr(h.get("ders", "")) != e_ders_low or _norm_ogr(h.get("ogretmen", "")) != e_ogr_low:
                    eslesme = False
                    break
                    
            if eslesme:
                if sinif_degisti:
                    for p in eski_parcalar:
                        for k_prog in list(prog[g][s].keys()):
                            if _to_lower_tr(k_prog) == _to_lower_tr(p):
                                prog[g][s][k_prog] = None
                    for p in yeni_parcalar:
                        prog[g][s][p] = {"ders": y_ders_orj, "ogretmen": y_ogr_orj}
                else:
                    for p in eski_parcalar:
                        for k_prog in list(prog[g][s].keys()):
                            if _to_lower_tr(k_prog) == _to_lower_tr(p):
                                if prog[g][s][k_prog]:
                                    prog[g][s][k_prog] = {"ders": y_ders_orj, "ogretmen": y_ogr_orj}
                                    
    for d in kalan:
        d_parcalar = [_to_lower_tr(x) for x in str(d.get("sinif", "")).split("+") if x.strip()]
        if set(d_parcalar) == set(e_snf_list) and _to_lower_tr(d.get("ders", "")) == e_ders_low and _norm_ogr(d.get("ogretmen", "")) == e_ogr_low:
            d["sinif"] = str(yeni_snf).strip()
            d["ders"] = y_ders_orj
            d["ogretmen"] = y_ogr_orj
            
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})

def _prog_senkronize(df):
    """Excel ile program matrisini harf ve boşluklara takılmadan kusursuz eşitler."""
    prog = prog_normalize(state.get("uretilen_program"))
    kalan = state.get("uretilen_kalan") or []
    
    if not prog: 
        prog = {}

    cols = list(df.columns)
    c_snf = _sutun_bul(cols, ["sinif", "class"])
    c_ders = _sutun_bul(cols, ["ders", "lesson", "subject"])
    c_ogr = _sutun_bul(cols, ["ogretmen", "teacher", "instructor"])
    c_saat = _sutun_bul(cols, ["saat", "hour"])

    hedef_saat = {}
    orijinal_isimler = {} 
    
    for _, r in df.iterrows():
        snflar_orj = [x.strip() for x in str(r[c_snf]).split("+") if x.strip()]
        snflar_low = sorted([_to_lower_tr(x) for x in snflar_orj])
        drs_orj = str(r[c_ders]).strip()
        ogr_orj = str(r[c_ogr]).strip()
        saat = int(float(str(r[c_saat]).strip() or 0))
        if not snflar_orj or not drs_orj or saat <= 0: continue
        
        snf_key_low = " + ".join(snflar_low)
        drs_low = _to_lower_tr(drs_orj)
        ogr_low = _norm_ogr(ogr_orj)
        key_low = (snf_key_low, drs_low, ogr_low)
        
        hedef_saat[key_low] = hedef_saat.get(key_low, 0) + saat
        orijinal_isimler[key_low] = (" + ".join(sorted(snflar_orj)), drs_orj, ogr_orj)

    mevcut_saat = {}
    islenen_hucreler = set()
    
    for g in range(get_max_gun()):
        for s in range(1, get_max_saat() + 1):
            if s not in prog.get(g, {}): continue
            
            for snf_orj, h in prog[g][s].items():
                if h and (g, s, snf_orj) not in islenen_hucreler:
                    grup_orj = [snf_orj]
                    grup_low = [_to_lower_tr(snf_orj)]
                    
                    h_drs_low = _to_lower_tr(h.get("ders", ""))
                    h_ogr_low = _norm_ogr(h.get("ogretmen", ""))
                    
                    for diger_snf, diger_h in prog[g][s].items():
                        if diger_snf != snf_orj and diger_h and _to_lower_tr(diger_h.get("ders", "")) == h_drs_low and _norm_ogr(diger_h.get("ogretmen", "")) == h_ogr_low:
                            grup_orj.append(diger_snf)
                            grup_low.append(_to_lower_tr(diger_snf))
                            
                    snf_key_low = " + ".join(sorted(grup_low))
                    key_low = (snf_key_low, h_drs_low, h_ogr_low)
                    
                    mevcut_saat[key_low] = mevcut_saat.get(key_low, 0) + 1
                    
                    for grp_snf in grup_orj:
                        islenen_hucreler.add((g, s, grp_snf))
                        
    kalan_saat = {}
    for d in kalan:
        snf_low = sorted([_to_lower_tr(x) for x in str(d.get("sinif", "")).split("+") if x.strip()])
        snf_key_low = " + ".join(snf_low)
        drs_low = _to_lower_tr(d.get("ders", ""))
        ogr_low = _norm_ogr(d.get("ogretmen", ""))
        key_low = (snf_key_low, drs_low, ogr_low)
        kalan_saat[key_low] = kalan_saat.get(key_low, 0) + int(d.get("saat", 1) if "saat" in d else d.get("blok", 1))

    tum_anahtarlar = set(mevcut_saat.keys()).union(set(kalan_saat.keys()))
    
    for key_low in tum_anahtarlar:
        mevcut_adet = mevcut_saat.get(key_low, 0)
        kalan_adet = kalan_saat.get(key_low, 0)
        total = mevcut_adet + kalan_adet
        fark = total - hedef_saat.get(key_low, 0)
        
        if fark > 0:
            snf_key_low, drs_low, ogr_low = key_low
            snf_liste_low = snf_key_low.split(" + ")
            
            yeni_kalan = []
            silinecek = fark
            for d in kalan:
                d_snf_low = " + ".join(sorted([_to_lower_tr(x) for x in str(d.get("sinif", "")).split("+") if x.strip()]))
                d_drs_low = _to_lower_tr(d.get("ders", ""))
                d_ogr_low = _norm_ogr(d.get("ogretmen", ""))
                
                if silinecek > 0 and d_snf_low == snf_key_low and d_drs_low == drs_low and d_ogr_low == ogr_low:
                    kart_saati = int(d.get("saat", 1) if "saat" in d else d.get("blok", 1))
                    if silinecek >= kart_saati:
                        silinecek -= kart_saati
                    else:
                        d["saat"] = kart_saati - silinecek
                        if "blok" in d: d["blok"] = kart_saati - silinecek
                        yeni_kalan.append(d)
                        silinecek = 0
                else:
                    yeni_kalan.append(d)
            kalan = yeni_kalan
            fark = silinecek
            
            if fark > 0:
                for g in range(get_max_gun()-1, -1, -1):
                    for s in range(get_max_saat(), 0, -1):
                        if fark <= 0: break
                        if s not in prog.get(g, {}): continue
                        
                        eslesen_siniflar_orj = []
                        for snf_orj, h in prog[g][s].items():
                            if h and _to_lower_tr(h.get("ders", "")) == drs_low and _norm_ogr(h.get("ogretmen", "")) == ogr_low:
                                if _to_lower_tr(snf_orj) in snf_liste_low:
                                    eslesen_siniflar_orj.append(snf_orj)
                                    
                        if len(eslesen_siniflar_orj) == len(snf_liste_low):
                            for x in eslesen_siniflar_orj:
                                prog[g][s][x] = None
                            fark -= 1
                            
    for key_low, target in hedef_saat.items():
        mevcut = mevcut_saat.get(key_low, 0) + kalan_saat.get(key_low, 0)
        eksik = target - mevcut
        if eksik > 0:
            snf_orj, drs_orj, ogr_orj = orijinal_isimler[key_low]
            for _ in range(eksik):
                kalan.append({
                    "sinif": snf_orj,
                    "ders": drs_orj,
                    "ogretmen": ogr_orj,
                    "saat": 1,
                    "blok": 1
                })
                
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})
    state.havuzu_sirala()

@app.post("/api/ders-yuku/satir")
def ders_yuku_satir(satir: DersYukuSatir):
    df = state.get("df_ders_yuku")
    if df is None:
        df = pd.DataFrame(columns=['Sınıf', 'Ders Adı', 'Öğretmen', 'Haftalık Saat'])

    cols   = list(df.columns)
    c_snf  = _sutun_bul(cols, ["sinif", "class"])
    c_ders = _sutun_bul(cols, ["ders", "lesson"])
    c_ogr  = _sutun_bul(cols, ["ogretmen", "teacher"])
    c_saat = _sutun_bul(cols, ["saat", "hour"])
    
    yeni = {c_snf: satir.sinif, c_ders: satir.ders_adi,
            c_ogr: satir.ogretmen, c_saat: satir.haftalik_saat}

    if satir.idx is None:
        df = pd.concat([df, pd.DataFrame([yeni])], ignore_index=True)
        state.set("df_ders_yuku", df)
        state.listeleri_guncelle()
        _prog_senkronize(df)
    else:
        if satir.idx in df.index:
            eski_snf  = str(df.loc[satir.idx, c_snf]).strip()
            eski_ders = str(df.loc[satir.idx, c_ders]).strip()
            eski_ogr  = str(df.loc[satir.idx, c_ogr]).strip()
            
            df.loc[satir.idx, c_snf]  = satir.sinif
            df.loc[satir.idx, c_ders] = satir.ders_adi
            df.loc[satir.idx, c_ogr]  = satir.ogretmen
            df.loc[satir.idx, c_saat] = satir.haftalik_saat
            state.set("df_ders_yuku", df)
            state.listeleri_guncelle()
            
            _prog_satir_guncelle(eski_snf, satir.sinif, eski_ders, satir.ders_adi, eski_ogr, satir.ogretmen)
            _prog_senkronize(df)
        else:
            df = pd.concat([df, pd.DataFrame([yeni])], ignore_index=True)
            state.set("df_ders_yuku", df)
            state.listeleri_guncelle()
            _prog_senkronize(df)

    _kaydet_ve_yedekle()
    return {"mesaj": "Satır güncellendi.", "satirlar": df_to_records(state.get("df_ders_yuku"))}

@app.delete("/api/ders-yuku/satir")
def ders_yuku_sil(idler: List[int]):
    df = state.get("df_ders_yuku")
    if df is None:
        raise HTTPException(400, "Tablo boş.")
    gercek_idler = [i for i in idler if i in df.index]
    df = df.drop(index=gercek_idler).reset_index(drop=True)
    state.set("df_ders_yuku", df)
    state.listeleri_guncelle()
    _prog_senkronize(df)
    return {"mesaj": f"{len(gercek_idler)} satır silindi.", "satirlar": df_to_records(df)}

@app.post("/api/ders-yuku/sablon-kopyala")
def sablon_kopyala(istek: SablonKopyala):
    df = state.get("df_ders_yuku")
    if df is None or df.empty:
        raise HTTPException(400, "Tablo boş.")
    kaynak_df = df[df['Sınıf'] == istek.kaynak_sinif].copy()
    if kaynak_df.empty:
        raise HTTPException(404, f"'{istek.kaynak_sinif}' sınıfı bulunamadı.")
    kaynak_df['Sınıf'] = istek.hedef_sinif
    yeni_df = pd.concat([df, kaynak_df], ignore_index=True)
    state.set("df_ders_yuku", yeni_df)
    state.listeleri_guncelle()
    return {"mesaj": "Şablon kopyalandı.", "satirlar": df_to_records(yeni_df)}

@app.post("/api/ders-yuku/yapistir")
def yapistir(istek: DersPanoYapistir):
    df = state.get("df_ders_yuku")
    if df is None:
        df = pd.DataFrame(columns=['Sınıf', 'Ders Adı', 'Öğretmen', 'Haftalık Saat'])

    cols   = list(df.columns)
    c_snf  = _sutun_bul(cols, ["sinif", "class"])
    c_ders = _sutun_bul(cols, ["ders", "lesson"])
    c_ogr  = _sutun_bul(cols, ["ogretmen", "teacher"])
    c_saat = _sutun_bul(cols, ["saat", "hour"])

    def _al(d, *anahtarlar):
        for k in anahtarlar:
            if k in d and str(d[k]) not in ("", "None", "nan"):
                return d[k]
        return ""

    yeni_satirlar = []
    for d in istek.pano:
        sinif_val = istek.hedef_sinif if istek.hedef_sinif else _al(d, "Sınıf", "Sinif", "sinif")
        ders_val  = _al(d, "Ders Adı", "Ders Adi", "ders_adi", "ders")
        ogr_val   = _al(d, "Öğretmen", "Ogretmen", "ogretmen")
        saat_val  = _al(d, "Haftalık Saat", "Haftalik Saat", "haftalik_saat", "saat")
        try:
            saat_int = int(float(str(saat_val))) if saat_val else 1
        except Exception:
            saat_int = 1
        yeni_satirlar.append({c_snf: sinif_val, c_ders: ders_val, c_ogr: ogr_val, c_saat: saat_int})

    if not yeni_satirlar:
        raise HTTPException(400, "Yapıştırılacak satır bulunamadı.")

    yeni_df = pd.concat([df, pd.DataFrame(yeni_satirlar)], ignore_index=True)
    state.set("df_ders_yuku", yeni_df)
    state.listeleri_guncelle()
    
    _prog_senkronize(yeni_df)
    return {"mesaj": f"{len(yeni_satirlar)} satır yapıştırıldı.", "satirlar": df_to_records(yeni_df)}

@app.get("/api/kisitlama")
def kisitlama_getir():
    state.saatleri_hesapla()
    s = state.snapshot()
    return {
        "zaman_ayarlari":        s["zaman_ayarlari"],
        "ogretmen_izinleri":     {k: [list(t) for t in v] for k, v in s["ogretmen_izinleri"].items()},
        "sinif_izinleri":        {k: [list(t) for t in v] for k, v in s["sinif_izinleri"].items()},
        "aktif_saat_etiketleri": s["aktif_saat_etiketleri"],
        "ogretmen_listesi":      s["ogretmen_listesi"],
        "sinif_listesi":         s["sinif_listesi"],
        "gelismis_ayarlar":      s.get("gelismis_ayarlar", {}),
    }

@app.post("/api/kisitlama-guncelle")
def kisitlama_guncelle(istek: KisitlamaGuncelle):
    state.set("zaman_ayarlari", istek.zaman_ayarlari.dict())
    state.set("ogretmen_izinleri", izin_listesine_cevir(istek.ogretmen_izinleri))
    state.set("sinif_izinleri",    izin_listesine_cevir(istek.sinif_izinleri))
    
    if istek.gelismis_ayarlar:
        state.set("gelismis_ayarlar", istek.gelismis_ayarlar.dict())
    
    state.saatleri_hesapla()
    _kaydet_ve_yedekle()
    
    return {
        "mesaj": "Kısıtlamalar kaydedildi.",
        "aktif_saat_etiketleri": state.get("aktif_saat_etiketleri"),
        "zaman_ayarlari": state.get("zaman_ayarlari")
    }

# ════════════════════════════════════════════════════════════════════════════
#  DERS YÜKÜ / PROGRAM SENKRONİZASYON YARDIMCILARI
# ════════════════════════════════════════════════════════════════════════════

def _yeni_dersleri_havuza_ekle(satirlar, c_snf, c_ders, c_ogr):
    prog = prog_normalize(state.get("uretilen_program"))
    if prog is None:
        return
    kalan = list(state.get("uretilen_kalan") or [])
    for d in satirlar:
        snf = str(d.get(c_snf, "") or "").strip()
        ders = str(d.get(c_ders, "") or "").strip()
        ogr = str(d.get(c_ogr, "") or "").strip()
        if snf and ders and snf not in ("nan", ""):
            kalan.append({"sinif": snf, "ders": ders, "ogretmen": ogr, "blok": 1})
    state.set("uretilen_kalan", kalan)
    state.havuzu_sirala()

def _prog_ders_guncelle(eski_ders, yeni_ders, sinif=""):
    """Küresel ders adı düzenlemesinde matrisi ve havuzu harf duyarsız olarak yerinde günceller."""
    prog = prog_normalize(state.get("uretilen_program"))
    if not prog:
        return
    
    sinif_parcalari_low = [_to_lower_tr(x) for x in sinif.split("+") if x.strip()] if sinif else []
    e_ders_low = _to_lower_tr(eski_ders)
    y_ders_orj = str(yeni_ders).strip()
    
    for g in range(get_max_gun()):
        for s in range(1, get_max_saat() + 1):
            if s not in prog.get(g, {}):
                continue
            for snf_prog, h in list(prog[g][s].items()):
                if not h:
                    continue
                if sinif and _to_lower_tr(snf_prog) not in sinif_parcalari_low:
                    continue
                if _to_lower_tr(h.get("ders", "")) == e_ders_low:
                    h["ders"] = y_ders_orj
                            
    kalan = state.get("uretilen_kalan") or []
    for d in kalan:
        if _to_lower_tr(d.get("ders", "")) == e_ders_low:
            d_sinif_parcalari_low = [_to_lower_tr(x) for x in str(d.get("sinif", "")).split("+") if x.strip()]
            if not sinif or any(sp in sinif_parcalari_low for sp in d_sinif_parcalari_low):
                d["ders"] = y_ders_orj
                
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})

def _prog_ogretmen_guncelle(eski_ogr, yeni_ogr):
    prog = prog_normalize(state.get("uretilen_program"))
    if not prog:
        return
    
    e_ogr_low = _to_lower_tr(eski_ogr)
    
    def _guncelle(v):
        parts = [p.strip() for p in str(v).split("+") if p.strip()]
        yeni_parts = [(str(yeni_ogr).strip() if _to_lower_tr(p) == e_ogr_low else p) for p in parts]
        return " + ".join(yeni_parts)
        
    for g in range(get_max_gun()):
        for s in range(1, get_max_saat() + 1):
            if s not in prog.get(g, {}):
                continue
            for snf_prog, h in list(prog[g][s].items()):
                if h and h.get("ogretmen"):
                    h["ogretmen"] = _guncelle(h["ogretmen"])
                    
    kalan = state.get("uretilen_kalan") or []
    for d in kalan:
        if d.get("ogretmen"):
            d["ogretmen"] = _guncelle(d["ogretmen"])
            
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})

# ════════════════════════════════════════════════════════════════════════════
#  KİLİTLİ DERS YÖNETİMİ
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/kisitlama/kilitli")
def kilitli_getir():
    return {"kilitler": state.get("kilitli_slotlar") or {}}

@app.post("/api/kisitlama/kilitli-kaydet")
def kilitli_kaydet(istek: KilitliKaydet):
    df = state.get("df_ders_yuku")
    if df is None or df.empty:
        raise HTTPException(400, "Önce Excel yüklemelisiniz.")

    cols = list(df.columns)
    c_snf = _sutun_bul(cols, ["sinif", "class"])
    c_drs = _sutun_bul(cols, ["ders", "lesson"])
    c_sat = _sutun_bul(cols, ["saat", "hour"])

    islenmis_kilitler = istek.kilitler
    ders_sayac = {}
    for v in islenmis_kilitler.values():
        if v.get("tip") == "sinif":
            k_isim, k_ders = v.get("isim"), v.get("ders")
            sayac_key = (k_isim, k_ders)
            ders_sayac[sayac_key] = ders_sayac.get(sayac_key, 0) + 1
            target = df[
                (df[c_snf].apply(lambda x: k_isim in [i.strip() for i in str(x).split('+')])) &
                (df[c_drs].str.strip().str.lower() == k_ders.strip().lower())
            ]
            if not target.empty:
                limit = int(float(str(target.iloc[0][c_sat])))
                if ders_sayac[sayac_key] > limit:
                    raise HTTPException(400, f"HATA: '{k_isim}' sınıfı için '{k_ders}' dersi limiti ({limit} saat) aşıldı!")

    state.set("kilitli_slotlar", islenmis_kilitler)
    _kaydet_ve_yedekle()
    return {"mesaj": "Kilitler kaydedildi.", "sayi": len(islenmis_kilitler)}

# ════════════════════════════════════════════════════════════════════════════
#  ÖĞRETMEN / SINIF EKLE – SİL
# ════════════════════════════════════════════════════════════════════════════

@app.post("/api/kisitlama/ogretmen-ekle")
def ogretmen_ekle(istek: AdIstek):
    ad = istek.ad.strip()
    if not ad:
        raise HTTPException(400, "Öğretmen adı boş olamaz.")
    with state._lock:
        liste = state._data["ogretmen_listesi"]
        if ad in liste:
            raise HTTPException(409, f"'{ad}' zaten mevcut.")
        liste.append(ad)
        state._data["ogretmen_listesi"] = sorted(liste, key=_dogal_sirala)
    return {"mesaj": f"'{ad}' eklendi.", "ogretmen_listesi": state.get("ogretmen_listesi")}

@app.post("/api/kisitlama/ogretmen-sil")
def ogretmen_sil(istek: AdIstek):
    ad = istek.ad.strip()
    df = state.get("df_ders_yuku")
    if df is not None and not df.empty:
        cols = list(df.columns)
        c_ogr = _sutun_bul(cols, ["ogretmen", "teacher"])
        mask = df[c_ogr].apply(lambda v: ad in [x.strip() for x in str(v).split("+")] if isinstance(v, str) else False)
        df = df[~mask].reset_index(drop=True)
        state.set("df_ders_yuku", df)

    prog = prog_normalize(state.get("uretilen_program"))
    if prog:
        for g in range(get_max_gun()):
            for s in range(1, get_max_saat() + 1):
                for snf, h in list(prog[g][s].items()):
                    if h and ad in [o.strip() for o in str(h.get("ogretmen", "")).split("+")]:
                        prog[g][s][snf] = None
        state.set("uretilen_program", prog)

    kalan = [d for d in (state.get("uretilen_kalan") or [])
             if ad not in [o.strip() for o in str(d.get("ogretmen", "")).split("+")]]
    state.set("uretilen_kalan", kalan)

    with state._lock:
        state._data["ogretmen_listesi"] = [o for o in state._data["ogretmen_listesi"] if o != ad]
        state._data["ogretmen_izinleri"].pop(ad, None)
    state.listeleri_guncelle()
    return {"mesaj": f"'{ad}' öğretmeni silindi."}

@app.post("/api/kisitlama/sinif-ekle")
def sinif_ekle(istek: AdIstek):
    ad = istek.ad.strip()
    if not ad:
        raise HTTPException(400, "Sınıf adı boş olamaz.")
    with state._lock:
        liste = state._data["sinif_listesi"]
        if ad in liste:
            raise HTTPException(409, f"'{ad}' zaten mevcut.")
        liste.append(ad)
        state._data["sinif_listesi"] = sorted(liste, key=_dogal_sirala)
    return {"mesaj": f"'{ad}' eklendi.", "sinif_listesi": state.get("sinif_listesi")}

@app.post("/api/kisitlama/sinif-sil")
def sinif_sil(istek: AdIstek):
    ad = istek.ad.strip()
    df = state.get("df_ders_yuku")
    if df is not None and not df.empty:
        cols = list(df.columns)
        c_snf = _sutun_bul(cols, ["sinif", "class"])
        mask = df[c_snf].apply(lambda v: ad in [x.strip() for x in str(v).split("+")] if isinstance(v,str) else False)
        df = df[~mask].reset_index(drop=True)
        state.set("df_ders_yuku", df)

    prog = prog_normalize(state.get("uretilen_program"))
    if prog:
        for g in range(get_max_gun()):
            for s in range(1, get_max_saat() + 1):
                if ad in prog[g][s]:
                    prog[g][s][ad] = None
        state.set("uretilen_program", prog)

    kalan = [d for d in (state.get("uretilen_kalan") or [])
             if ad not in [x.strip() for x in d.get("sinif","").split("+")]]
    state.set("uretilen_kalan", kalan)

    with state._lock:
        state._data["sinif_listesi"] = [s for s in state._data["sinif_listesi"] if s != ad]
        state._data["sinif_izinleri"].pop(ad, None)
    state.listeleri_guncelle()
    return {"mesaj": f"'{ad}' sınıfı silindi."}

# ════════════════════════════════════════════════════════════════════════════
#  YÖNETİM – ÖĞRETMEN / DERS / SINIF İSİM DEĞİŞTİRME
# ════════════════════════════════════════════════════════════════════════════

class IsimDegistir(BaseModel):
    eski: str
    yeni: str

@app.post("/api/yonetim/ogretmen-duzenle")
def ogretmen_duzenle(istek: IsimDegistir):
    eski = istek.eski.strip()
    yeni = istek.yeni.strip()
    if not yeni: raise HTTPException(400, "Yeni ad boş olamaz.")
    df = state.get("df_ders_yuku")
    if df is not None and not df.empty:
        c = _sutun_bul(list(df.columns), ["ogretmen","teacher"])
        df[c] = df[c].apply(lambda v: " + ".join([yeni if p.strip()==eski else p.strip() for p in str(v).split("+")]) if isinstance(v,str) else v)
        state.set("df_ders_yuku", df)
    _prog_ogretmen_guncelle(eski, yeni)
    with state._lock:
        ogr = state._data["ogretmen_listesi"]
        if eski in ogr: ogr[ogr.index(eski)] = yeni
        izin = state._data["ogretmen_izinleri"]
        if eski in izin: izin[yeni] = izin.pop(eski)
    state.listeleri_guncelle()
    return {"mesaj": f"'{eski}' → '{yeni}'", "ogretmen_listesi": state.get("ogretmen_listesi")}

@app.post("/api/yonetim/ders-ekle")
def ders_ekle(istek: AdIstek):
    ad = istek.ad.strip()
    if not ad:
        raise HTTPException(400, "Ders adı boş olamaz.")
    with state._lock:
        liste = state._data.get("ders_listesi") or []
        if ad in liste:
            raise HTTPException(409, f"'{ad}' zaten mevcut.")
        liste.append(ad)
        state._data["ders_listesi"] = sorted(liste, key=_dogal_sirala)
    return {"mesaj": f"'{ad}' eklendi.", "ders_listesi": state.get("ders_listesi")}

@app.post("/api/yonetim/ders-duzenle")
def ders_duzenle(istek: IsimDegistir):
    eski = istek.eski.strip()
    yeni = istek.yeni.strip()
    if not yeni: 
        raise HTTPException(400, "Yeni ad boş olamaz.")
        
    df = state.get("df_ders_yuku")
    if df is not None and not df.empty:
        c = _sutun_bul(list(df.columns), ["ders", "lesson"])
        df[c] = df[c].apply(lambda v: yeni if _to_lower_tr(v) == _to_lower_tr(eski) else v)
        state.set("df_ders_yuku", df)
        
        _prog_ders_guncelle(eski, yeni)
        _prog_senkronize(df)
        
    with state._lock:
        drs = state._data["ders_listesi"]
        for idx, d in enumerate(drs):
            if _to_lower_tr(d) == _to_lower_tr(eski):
                drs[idx] = yeni
                
    state.listeleri_guncelle()
    _kaydet_ve_yedekle()
    return {"mesaj": f"'{eski}' → '{yeni}'", "ders_listesi": state.get("ders_listesi")}

@app.post("/api/yonetim/sinif-duzenle")
def sinif_duzenle(istek: IsimDegistir):
    eski = istek.eski.strip()
    yeni = istek.yeni.strip()
    if not yeni: raise HTTPException(400, "Yeni ad boş olamaz.")
    
    df = state.get("df_ders_yuku")
    if df is not None and not df.empty:
        c = _sutun_bul(list(df.columns), ["sinif","class"])
        df[c] = df[c].apply(lambda v: " + ".join([yeni if p.strip()==eski else p.strip() for p in str(v).split("+")]) if isinstance(v,str) else v)
        state.set("df_ders_yuku", df)
        
    prog = prog_normalize(state.get("uretilen_program"))
    if prog:
        e_snf_low = _to_lower_tr(eski)
        for g in range(get_max_gun()):
            for s in range(1, get_max_saat() + 1):
                if s not in prog.get(g, {}): continue
                for k_prog in list(prog[g][s].keys()):
                    if _to_lower_tr(k_prog) == e_snf_low:
                        prog[g][s][yeni] = prog[g][s].pop(k_prog)
        state.set("uretilen_program", prog)
        
    kalan = state.get("uretilen_kalan") or []
    for d in kalan:
        d["sinif"] = " + ".join([yeni if p.strip()==eski else p.strip() for p in d.get("sinif","").split("+")])
    state.set("uretilen_kalan", kalan)
    
    with state._lock:
        snf = state._data["sinif_listesi"]
        if eski in snf: snf[snf.index(eski)] = yeni
        izin = state._data["sinif_izinleri"]
        if eski in izin: izin[yeni] = izin.pop(eski)
        
    state.listeleri_guncelle()
    return {"mesaj": f"'{eski}' → '{yeni}'", "sinif_listesi": state.get("sinif_listesi")}

# ════════════════════════════════════════════════════════════════════════════
#  DERS YÜKÜ EXCEL İNDİR
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/ders-yuku/excel-indir")
def ders_yuku_excel_indir():
    df = state.get("df_ders_yuku")
    if df is None or df.empty:
        raise HTTPException(400, "Ders yükü boş.")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Ders Yükü")
    buf.seek(0)
    return StreamingResponse(buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=ders_yuku.xlsx"})

# ════════════════════════════════════════════════════════════════════════════
#  PROGRAM OLUŞTURMA (MOTOR)
# ════════════════════════════════════════════════════════════════════════════

@app.post("/api/program/olustur")
def program_olustur(istek: ProgramOlusturIstek = None):
    if istek is None:
        istek = ProgramOlusturIstek()
    s = state.snapshot()
    if s["df_ders_yuku"] is None or s["df_ders_yuku"].empty:
        raise HTTPException(400, "Önce Excel yükleyin.")
    if not ORTOOLS_VAR:
        raise HTTPException(500, "OR-Tools kurulu değil. Komut: pip install ortools")
    if s["motor_calisiyor"]:
        raise HTTPException(409, "Motor zaten çalışıyor, lütfen bekleyin.")

    sure = max(5, min(1800, istek.sure_saniye or 600))
    state.set("motor_calisiyor", True)
    state.set("son_motor_sonuc", None)
    state.set("hata_mesaji",  "")

    df_copy      = s["df_ders_yuku"].copy()
    siniflar     = list(s["sinif_listesi"])
    ogretmenler  = list(s["ogretmen_listesi"])
    ogr_izin     = dict(s["ogretmen_izinleri"])
    snf_izin     = dict(s["sinif_izinleri"])
    kilitli_ham  = state.get("kilitli_slotlar") or {}
    kilitli_liste= []

    cols = list(df_copy.columns)
    c_snf  = _sutun_bul(cols, ["sinif", "class"])
    c_ders = _sutun_bul(cols, ["ders", "lesson"])
    c_ogr  = _sutun_bul(cols, ["ogretmen", "teacher"])
    c_saat = _sutun_bul(cols, ["saat", "hour"])

    def _to_lower_tr(s):
        return str(s).strip().replace('I', 'ı').replace('İ', 'i').lower()

    islenen_kilit_set = set()
    islenen_mufredat_saatleri = set()
    for v in kilitli_ham.values():
        try:
            k_ders = _to_lower_tr(v.get("ders", ""))
            k_isim = _to_lower_tr(v.get("isim", ""))
            k_tip  = v.get("tip")
            g, st  = int(v["gun"]), int(v["saat"])
            if not k_ders or not k_isim: continue

            paydas_siniflar = set()
            paydas_hocalar = set()
            bulunan_satir_idx = -1
            k_ogr_front = v.get("kilitliOgr")

            for idx2, row in df_copy.iterrows():
                db_ders = _to_lower_tr(row[c_ders])
                db_snf_list = [_to_lower_tr(sn) for sn in str(row[c_snf]).split('+')]
                db_ogr_list = [_to_lower_tr(og) for og in str(row[c_ogr]).split('+')]
                mevcut = int(float(str(row[c_saat])))

                ders_es = (k_ders in db_ders) or (db_ders in k_ders)
                kaynak_es = (k_isim in db_snf_list) if k_tip == "sinif" else (k_isim in db_ogr_list)

                if ders_es and kaynak_es and mevcut > 0:
                    if k_ogr_front and k_ogr_front != "None":
                        front_ogr_lower = _to_lower_tr(k_ogr_front)
                        if not any(front_ogr_lower in o for o in db_ogr_list):
                            continue 
                    paydas_siniflar.update([sn.strip() for sn in str(row[c_snf]).split('+')])
                    paydas_hocalar.update([og.strip() for og in str(row[c_ogr]).split('+')])
                    bulunan_satir_idx = idx2
                    break

            hedefler = []
            if bulunan_satir_idx != -1:
                for sn in paydas_siniflar: hedefler.append(("sinif", sn))
                for og in paydas_hocalar:  hedefler.append(("ogretmen", og))
            else:
                hedefler.append((k_tip, v.get("isim")))
                if k_ogr_front and k_ogr_front != "None":
                    hedefler.append(("ogretmen", k_ogr_front))

            for tur, ad in hedefler:
                if (tur, ad, g, st) not in islenen_kilit_set:
                    kilitli_liste.append({"kaynak_turu": tur, "kaynak_adi": ad, "gun": g, "saat": st, "ders": v.get("ders")})
                    islenen_kilit_set.add((tur, ad, g, st))

            if bulunan_satir_idx != -1 and (bulunan_satir_idx, g, st) not in islenen_mufredat_saatleri:
                df_copy.at[bulunan_satir_idx, c_saat] = int(float(str(df_copy.at[bulunan_satir_idx, c_saat]))) - 1
                islenen_mufredat_saatleri.add((bulunan_satir_idx, g, st))
        except Exception as e:
            print(f"[UYARI] Kilit hatası: {e}")

    def calistir():
        try:
            sonuc = program_hesapla(
                df_ders_yuku=df_copy,
                sinif_listesi=siniflar,
                ogretmen_listesi=ogretmenler,
                ogretmen_izinleri=ogr_izin,
                sinif_izinleri=snf_izin,
                sure_saniye=sure,
                kilitli_saatler=kilitli_liste,
                gelismis_ayarlar=s.get("gelismis_ayarlar", {}),
                zaman_ayarlari=s.get("zaman_ayarlari", {})
            )
            if sonuc["durum"] == "TAMAM":
                prog = sonuc["program"]
                for v in kilitli_ham.values():
                    try:
                        g, st = int(v["gun"]), int(v["saat"])
                        k_tip = v.get("tip", "sinif")
                        k_isim = str(v.get("isim", "")).strip()
                        k_ders = str(v.get("ders", "")).strip()
                        hoca_adi = v.get("kilitliOgr")

                        hedef_siniflar = []
                        if v.get("siniflar"):
                            hedef_siniflar = [s.strip() for s in v["siniflar"] if s.strip()]
                        elif k_tip == "sinif":
                            hedef_siniflar = [s.strip() for s in k_isim.split("+") if s.strip()]

                        if not hoca_adi or hoca_adi == "None" or not hedef_siniflar:
                            for idx2, row in df_copy.iterrows():
                                row_ders = _to_lower_tr(row[c_ders])
                                row_snfs = [_to_lower_tr(x) for x in str(row[c_snf]).split("+") if x.strip()]
                                row_ogrs = [_to_lower_tr(x) for x in str(row[c_ogr]).split("+") if x.strip()]

                                ders_uyusuyor = (_to_lower_tr(k_ders) in row_ders) or (row_ders in _to_lower_tr(k_ders))
                                if not ders_uyusuyor:
                                    continue

                                eslesti = False
                                if k_tip == "sinif" and any(_to_lower_tr(s) in row_snfs for s in (hedef_siniflar or [_to_lower_tr(k_isim)])):
                                    eslesti = True
                                elif k_tip == "ogretmen" and _to_lower_tr(k_isim) in row_ogrs:
                                    eslesti = True

                                if eslesti:
                                    if not hedef_siniflar:
                                        hedef_siniflar = [s.strip() for s in str(row[c_snf]).split("+") if s.strip()]
                                    if not hoca_adi or hoca_adi == "None":
                                        hoca_adi = str(row[c_ogr]).strip()
                                    break

                        if not hoca_adi or hoca_adi == "None":
                            hoca_adi = k_isim if k_tip == "ogretmen" else "İsimsiz"

                        if g in prog and st in prog[g]:
                            for snf in hedef_siniflar:
                                for prog_snf in list(prog[g][st].keys()):
                                    if _to_lower_tr(prog_snf) == _to_lower_tr(snf):
                                        prog[g][st][prog_snf] = {"ders": k_ders, "ogretmen": hoca_adi}
                    except Exception as e:
                        print(f"[KİLİT YAZMA HATA]: {e}")
                
                state.update({
                    "uretilen_program": prog,
                    "uretilen_kalan": sonuc["kalan"],
                })
                state.havuzu_sirala()
            state.update({
                "motor_calisiyor": False,
                "son_motor_sonuc": sonuc["durum"],
                "hata_mesaji": sonuc.get("hata_mesaji", ""),
            })
        except Exception as e:
            print(f"❌ Motor Hatası: {e}")
            state.update({
                "motor_calisiyor": False,
                "son_motor_sonuc": "HATA",
                "hata_mesaji": f"Sistemsel Hata: {str(e)}",
            })

    threading.Thread(target=calistir, daemon=True).start()
    return {"mesaj": f"Motor başlatıldı ({sure}s). /api/program/durum ile takip edin."}

@app.get("/api/program/durum")
def program_durum():
    s = state.snapshot()
    return {
        "motor_calisiyor": s["motor_calisiyor"],
        "program_var":     s["uretilen_program"] is not None,
        "kalan_sayisi":    len(s["uretilen_kalan"]),
        "son_sonuc":       s["son_motor_sonuc"],
        "hata_mesaji":     s.get("hata_mesaji", ""),
        "snapshot_var":    state.get("karma_oncesi_snapshot") is not None,
    }

@app.get("/api/program/veri")
def program_veri():
    s = state.snapshot()
    state.saatleri_hesapla()
    if s["uretilen_program"] is None:
        raise HTTPException(404, "Henüz program üretilmedi.")
    z = s.get("zaman_ayarlari") or {}
    return {
        "program":               s["uretilen_program"],
        "kalan":                 s["uretilen_kalan"],
        "hata_mesaji":           s.get("hata_mesaji", ""),
        "sinif_listesi":         s["sinif_listesi"],
        "ogretmen_listesi":      s["ogretmen_listesi"],
        "aktif_saat_etiketleri": state.get("aktif_saat_etiketleri"),
        "ogretmen_izinleri":     {k: [list(t) for t in v] for k, v in s["ogretmen_izinleri"].items()},
        "sinif_izinleri":        {k: [list(t) for t in v] for k, v in s["sinif_izinleri"].items()},
        "gunler":                z.get("gunler") or ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma"],
        "ders_sayisi":           z.get("ders_sayisi") or 8,
        "gun_saatleri":          z.get("gun_saatleri") or {},
    }

# ════════════════════════════════════════════════════════════════════════════
#  PROGRAMDA MANUEL DEĞİŞİKLİKLER (TAKAS, EZME & SLOT YÖNETİMİ)
# ════════════════════════════════════════════════════════════════════════════

import copy
from fastapi import HTTPException

@app.post("/api/program/takas")
def takas(istek: TakasIstek):
    """
    Aynı sınıfın kendi hücreleri arasında veya tıkla-takas ile yapılan ezme işlemi.
    Eski dersi ve çakışan tüm paydaşları (yeni gelen hocanın diğer meşguliyetleri dahil)
    güvenle havuza gönderir. Tam serbestlik modundadır, kilitler dahil her şey onaya bağlanır.
    """
    prog = prog_normalize(state.get("uretilen_program"))
    if prog is None:
        raise HTTPException(404, "Program bulunamadı.")

    # 1. Havuzun güncel durumunu listeye çekiyoruz
    kalan = list(state.get("uretilen_kalan") or [])

    gun1, saat1, sinif1 = istek.gun1, istek.saat1, istek.sinif
    gun2, saat2, sinif2 = istek.gun2, istek.saat2, istek.sinif

    kilitler = state.get("kilitli_slotlar") or {}
    def is_locked(gun, saat, sinif, ogretmen):
        for k_id, v in kilitler.items():
            try:
                kilit_gun = int(v.get("gun") or -1)
                kilit_saat = int(v.get("saat") or -1)
            except (ValueError, TypeError):
                continue

            if kilit_gun == gun and kilit_saat == saat:
                if v.get("tip") == "sinif" and v.get("isim") == sinif: return True
                if ogretmen:
                    for hoca in [h.strip() for h in ogretmen.split("+")]:
                        if v.get("tip") == "ogretmen" and v.get("isim") == hoca: return True
                        if v.get("kilitliOgr") == hoca: return True
        return False

    def birlesik_grup_bul(gun, saat, sinif):
        d = prog.get(gun, {}).get(saat, {}).get(sinif)
        if not d: return [sinif]
        d_low = _to_lower_tr(d.get("ders", ""))
        o_low = _norm_ogr(d.get("ogretmen", ""))
        return [k for k, v in prog.get(gun, {}).get(saat, {}).items() if v and _to_lower_tr(v.get("ders", "")) == d_low and _norm_ogr(v.get("ogretmen", "")) == o_low]

    grup1 = birlesik_grup_bul(gun1, saat1, sinif1)
    ders1 = copy.deepcopy(prog.get(gun1, {}).get(saat1, {}).get(sinif1))

    if not ders1:
        raise HTTPException(400, "Kaynak hücre boş, taşınacak ders yok.")

    for snf in grup1:
        if is_locked(gun1, saat1, snf, ders1.get("ogretmen")):
            raise HTTPException(403, f"Kaynak gruptaki ({snf}) ders kilitli, taşınamaz!")

    clear_classes = set()
    mesajlar = set()

    # Hedef konumda ezilecek doğrudan sınıfları ekiyoruz
    for snf in grup1:
        if prog.get(gun2, {}).get(saat2, {}).get(snf):
            clear_classes.add(snf)
            
    if prog.get(gun2, {}).get(saat2, {}).get(sinif2):
        clear_classes.add(sinif2)

    # TAM SERBESTLİK GÜNCELLEMESİ: Hem ezilen (eski) hocaların hem de yeni gelen (ders1) hocanın tüm çakışmalarını topluyoruz
    tüm_hocalar = set()
    
    # 1. Kaynak dersin (yeni gelen) hocalarını ekle
    if ders1.get("ogretmen"):
        for hoca in [h.strip() for h in ders1["ogretmen"].split("+") if h.strip()]:
            tüm_hocalar.add(hoca)
            
    # 2. Hedef konumda ezilecek olan eski hocaları ekle
    for snf in list(clear_classes):
        d_obj = prog.get(gun2, {}).get(saat2, {}).get(snf)
        if d_obj and d_obj.get("ogretmen"):
            for hoca in [h.strip() for h in d_obj["ogretmen"].split("+") if h.strip()]:
                tüm_hocalar.add(hoca)

    # 3. Bu hocalardan herhangi birinin o saatte başka bir sınıfta dersi varsa clear_classes listesine dahil et
    if tüm_hocalar:
        for snf_all, d_obj in prog.get(gun2, {}).get(saat2, {}).items():
            if d_obj and d_obj.get("ogretmen") and snf_all not in clear_classes:
                hocalars2 = [h.strip() for h in d_obj["ogretmen"].split("+") if h.strip()]
                if any(h in tüm_hocalar for h in hocalars2):
                    clear_classes.add(snf_all)
                    for h in tüm_hocalar:
                        if h in hocalars2:
                            mesajlar.add(f"{h} öğretmeni {gun2}. Gün {saat2}. Saatte {snf_all} sınıfında çakışıyor.")

    expanded_clear_classes = set()
    for snf in clear_classes:
        if snf not in expanded_clear_classes:
            g_conflict = birlesik_grup_bul(gun2, saat2, snf)
            expanded_clear_classes.update(g_conflict)

    # TAM SERBESTLİK GÜNCELLEMESİ: Kilitli hedef hücreleri 403 ile engellemek yerine onay uyarısına dahil ediyoruz
    for snf in expanded_clear_classes:
        d_conf = prog.get(gun2, {}).get(saat2, {}).get(snf)
        if is_locked(gun2, saat2, snf, d_conf.get("ogretmen") if d_conf else None):
            mesajlar.add(f"DİKKAT: {snf} sınıfındaki çakışan ders KİLİTLİDİR. Üzerine yazmayı seçtiniz.")

    unique_conflicts = {}
    for snf in expanded_clear_classes:
        d_obj = prog.get(gun2, {}).get(saat2, {}).get(snf)
        if d_obj:
            k = (d_obj.get("ders", ""), d_obj.get("ogretmen", ""))
            unique_conflicts.setdefault(k, set()).add(snf)

    if (unique_conflicts or mesajlar) and not istek.onay:
        prompt_msgs = []
        for (d_name, d_ogr), snfs in unique_conflicts.items():
            snf_str = " + ".join(sorted(list(snfs)))
            prompt_msgs.append(f"{snf_str} sınıfındaki {d_name} ({d_ogr}) dersi havuza alınacaktır.")
        for m in mesajlar:
            prompt_msgs.append(m)
        raise HTTPException(
            status_code=409,
            detail={
                "tip": "onay_gerekli",
                "mesaj": "Bu işlem sonucunda aşağıdaki dersler HAVUZA alınacaktır:\n" + "\n".join(set(prompt_msgs))
            }
        )

    # 2. Kaynak hücredeki A dersinin eski yerini temizliyoruz (Yukarısı boşalıyor)
    for snf in grup1:
        prog[gun1][saat1][snf] = None

    # 3. YENİ: Çakışan/Ezilen tüm B grubu derslerini güvenli bir şekilde HAVUZA fırlatıyoruz
    for (d_name, d_ogr), snfs in unique_conflicts.items():
        sinif_str = " + ".join(sorted(list(snfs), key=_dogal_sirala))
        kalan.append({
            "sinif": sinif_str,
            "ders": copy.deepcopy(d_name),
            "ogretmen": copy.deepcopy(d_ogr),
            "blok": 1
        })

    # Hedef hücredeki eski matris kayıtlarını sıfırlıyoruz
    for snf in expanded_clear_classes:
        prog[gun2][saat2][snf] = None

    # 4. A dersini hedef hücreye yerleştiriyoruz
    for snf in grup1:
        prog.setdefault(gun2, {}).setdefault(saat2, {})[snf] = ders1

    # State güncellemelerini tek seferde yapıp havuzu hizalıyoruz
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})
    state.havuzu_sirala()
    
    df = state.get("df_ders_yuku")
    if df is not None:
        _prog_senkronize(df)

    return {"mesaj": "Ders başarıyla yerleştirildi, çakışan dersler havuza aktarıldı.", "program": state.get("uretilen_program"), "kalan": state.get("uretilen_kalan")}


@app.post("/api/program/takas-farkli-sinif")
def takas_farkli_sinif(istek: TakasFarkliSinif):
    """
    Farklı iki sınıfın tıkla-takas ekranındaki kesişim ezme operasyonu.
    Tekil ve birleşik grupları koruyarak çakışan her şeyi havuza gönderir.
    Tam serbestlik modundadır, kilitler dahil her şey onaya bağlanır.
    """
    prog = prog_normalize(state.get("uretilen_program"))
    if prog is None:
        raise HTTPException(404, "Program bulunamadı.")

    # 1. Havuzun güncel durumunu listeye çekiyoruz
    kalan = list(state.get("uretilen_kalan") or [])

    gun1, saat1, sinif1 = istek.gun1, istek.saat1, istek.sinif1
    gun2, saat2, sinif2 = istek.gun2, istek.saat2, istek.sinif2

    kilitler = state.get("kilitli_slotlar") or {}
    def is_locked(gun, saat, sinif, ogretmen):
        for k_id, v in kilitler.items():
            try:
                kilit_gun = int(v.get("gun") or -1)
                kilit_saat = int(v.get("saat") or -1)
            except (ValueError, TypeError):
                continue

            if kilit_gun == gun and kilit_saat == saat:
                if v.get("tip") == "sinif" and v.get("isim") == sinif: return True
                if ogretmen:
                    for hoca in [h.strip() for h in ogretmen.split("+")]:
                        if v.get("tip") == "ogretmen" and v.get("isim") == hoca: return True
                        if v.get("kilitliOgr") == hoca: return True
        return False

    def birlesik_grup_bul(gun, saat, sinif):
        d = prog.get(gun, {}).get(saat, {}).get(sinif)
        if not d: return [sinif]
        d_low = _to_lower_tr(d.get("ders", ""))
        o_low = _norm_ogr(d.get("ogretmen", ""))
        return [k for k, v in prog.get(gun, {}).get(saat, {}).items() if v and _to_lower_tr(v.get("ders", "")) == d_low and _norm_ogr(v.get("ogretmen", "")) == o_low]

    grup1 = birlesik_grup_bul(gun1, saat1, sinif1)
    ders1 = copy.deepcopy(prog.get(gun1, {}).get(saat1, {}).get(sinif1))

    if not ders1:
        raise HTTPException(400, "Kaynak hücre boş, taşınacak ders yok.")

    for snf in grup1:
        if is_locked(gun1, saat1, snf, ders1.get("ogretmen")):
            raise HTTPException(403, f"Kaynak gruptaki ({snf}) ders kilitli, taşınamaz!")

    clear_classes = set()
    mesajlar = set()

    for snf in grup1:
        if prog.get(gun2, {}).get(saat2, {}).get(snf):
            clear_classes.add(snf)
            
    if prog.get(gun2, {}).get(saat2, {}).get(sinif2):
        clear_classes.add(sinif2)

    # TAM SERBESTLİK GÜNCELLEMESİ: Hem ezilen (eski) hocaların hem de yeni gelen (ders1) hocanın tüm çakışmalarını topluyoruz
    tüm_hocalar = set()
    
    # 1. Kaynak dersin (yeni gelen) hocalarını ekle
    if ders1.get("ogretmen"):
        for hoca in [h.strip() for h in ders1["ogretmen"].split("+") if h.strip()]:
            tüm_hocalar.add(hoca)
            
    # 2. Hedef konumda ezilecek olan eski hocaları ekle
    for snf in list(clear_classes):
        d_obj = prog.get(gun2, {}).get(saat2, {}).get(snf)
        if d_obj and d_obj.get("ogretmen"):
            for hoca in [h.strip() for h in d_obj["ogretmen"].split("+") if h.strip()]:
                tüm_hocalar.add(hoca)

    # 3. Bu hocalardan herhangi birinin o saatte başka bir sınıfta dersi varsa clear_classes listesine dahil et
    if tüm_hocalar:
        for snf_all, d_obj in prog.get(gun2, {}).get(saat2, {}).items():
            if d_obj and d_obj.get("ogretmen") and snf_all not in clear_classes:
                hocalars2 = [h.strip() for h in d_obj["ogretmen"].split("+") if h.strip()]
                if any(h in tüm_hocalar for h in hocalars2):
                    clear_classes.add(snf_all)
                    for h in tüm_hocalar:
                        if h in hocalars2:
                            mesajlar.add(f"{h} öğretmeni {gun2}. Gün {saat2}. Saatte {snf_all} sınıfında çakışıyor.")

    expanded_clear_classes = set()
    for snf in clear_classes:
        if snf not in expanded_clear_classes:
            g_conflict = birlesik_grup_bul(gun2, saat2, snf)
            expanded_clear_classes.update(g_conflict)

    # TAM SERBESTLİK GÜNCELLEMESİ: Kilitli hedef hücreleri 403 ile engellemek yerine onay uyarısına dahil ediyoruz
    for snf in expanded_clear_classes:
        d_conf = prog.get(gun2, {}).get(saat2, {}).get(snf)
        if is_locked(gun2, saat2, snf, d_conf.get("ogretmen") if d_conf else None):
            mesajlar.add(f"DİKKAT: {snf} sınıfındaki çakışan ders KİLİTLİDİR. Üzerine yazmayı seçtiniz.")

    unique_conflicts = {}
    for snf in expanded_clear_classes:
        d_obj = prog.get(gun2, {}).get(saat2, {}).get(snf)
        if d_obj:
            k = (d_obj.get("ders", ""), d_obj.get("ogretmen", ""))
            unique_conflicts.setdefault(k, set()).add(snf)

    if (unique_conflicts or mesajlar) and not istek.onay:
        prompt_msgs = []
        for (d_name, d_ogr), snfs in unique_conflicts.items():
            snf_str = " + ".join(sorted(list(snfs)))
            prompt_msgs.append(f"{snf_str} sınıfındaki {d_name} ({d_ogr}) dersi havuza alınacaktır.")
        for m in mesajlar:
            prompt_msgs.append(m)
        raise HTTPException(
            status_code=409,
            detail={
                "tip": "onay_gerekli",
                "mesaj": "Bu işlem sonucunda aşağıdaki dersler HAVUZA alınacaktır:\n" + "\n".join(set(prompt_msgs))
            }
        )

    # 2. A dersinin eski yerini boşaltıyoruz
    for snf in grup1:
        prog[gun1][saat1][snf] = None

    # 3. YENİ: Çakışan dersleri doğrudan toplu grup kartı olarak HAVUZA delege ediyoruz
    for (d_name, d_ogr), snfs in unique_conflicts.items():
        sinif_str = " + ".join(sorted(list(snfs), key=_dogal_sirala))
        kalan.append({
            "sinif": sinif_str,
            "ders": copy.deepcopy(d_name),
            "ogretmen": copy.deepcopy(d_ogr),
            "blok": 1
        })

    for snf in expanded_clear_classes:
        prog[gun2][saat2][snf] = None

    # 4. A dersini hedef konuma yazıyoruz
    for snf in grup1:
        prog.setdefault(gun2, {}).setdefault(saat2, {})[snf] = ders1

    # State ve havuz senkronizasyonu
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})
    state.havuzu_sirala()
    
    df = state.get("df_ders_yuku")
    if df is not None:
        _prog_senkronize(df)

    return {"mesaj": "Ders başarıyla yerleştirildi, çakışan dersler havuza aktarıldı.", "program": state.get("uretilen_program"), "kalan": state.get("uretilen_kalan")}
@app.post("/api/program/havuza-at")
def havuza_at(istek: TabloHavuzaAt):
    prog  = prog_normalize(state.get("uretilen_program"))
    kalan = state.get("uretilen_kalan") or []
    
    if prog is None:
        raise HTTPException(404, "Program yok.")
        
    h = prog.get(istek.gun, {}).get(istek.saat, {}).get(istek.sinif)
    if not h:
        raise HTTPException(400, "Bu hücrede ders yok.")
        
    kalan.append({
        "sinif":    istek.sinif,
        "ders":     h["ders"],
        "ogretmen": h["ogretmen"],
        "blok":     1
    })
    prog[istek.gun][istek.saat][istek.sinif] = None
    
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})
    state.havuzu_sirala()
    df = state.get("df_ders_yuku")
    if df is not None:
        _prog_senkronize(df)
    return {"mesaj": f"{istek.sinif} havuza atıldı."}
@app.post("/api/program/havuza-at-grup")
def havuza_at_grup(istek: GrupHavuzaAt):
    prog  = prog_normalize(state.get("uretilen_program"))
    kalan = state.get("uretilen_kalan") or []
    if prog is None:
        raise HTTPException(404, "Program yok.")
    ders_adi = None
    ogretmen = None
    for snf in istek.siniflar:
        h = prog[istek.gun][istek.saat].get(snf)
        if h:
            if ders_adi is None:
                ders_adi = h["ders"]
                ogretmen = h["ogretmen"]
            prog[istek.gun][istek.saat][snf] = None
    if ders_adi is None:
        raise HTTPException(400, "Bu hücrelerde ders yok.")
    sinif_str = " + ".join(sorted(istek.siniflar, key=_dogal_sirala))
    kalan.append({
        "sinif":    sinif_str,
        "ders":     ders_adi,
        "ogretmen": ogretmen,
        "blok":     1
    })
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})
    state.havuzu_sirala()
    return {"mesaj": f"Grup havuza atıldı: {sinif_str}"}
class SinifHavuzaAtIstek(BaseModel):
    sinif: str
@app.post("/api/program/sinif-havuza-at")
def sinif_havuza_at(istek: SinifHavuzaAtIstek):
    prog = prog_normalize(state.get("uretilen_program"))
    kalan = list(state.get("uretilen_kalan") or [])
    if prog is None:
        raise HTTPException(404, "Program bulunamadı.")
    
    sinif = istek.sinif
    islenen = set()
    for g in range(get_max_gun()):
        for s in range(1, get_max_saat() + 1):
            if s not in prog.get(g, {}):
                continue
            cell = prog[g][s].get(sinif)
            if cell and (g, s, sinif) not in islenen:
                m_ders = _to_lower_tr(cell.get("ders", ""))
                m_ogr = _norm_ogr(cell.get("ogretmen", ""))
                ortak_siniflar = [
                    k for k, v in prog[g][s].items()
                    if v and _to_lower_tr(v.get("ders", "")) == m_ders and _norm_ogr(v.get("ogretmen", "")) == m_ogr
                ]
                sinif_str = " + ".join(sorted(ortak_siniflar, key=_dogal_sirala))
                kalan.append({
                    "sinif": sinif_str,
                    "ders": cell["ders"],
                    "ogretmen": cell["ogretmen"],
                    "blok": 1
                })
                for osnf in ortak_siniflar:
                    islenen.add((g, s, osnf))
                    prog[g][s][osnf] = None

    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})
    state.havuzu_sirala()
    df = state.get("df_ders_yuku")
    if df is not None:
        _prog_senkronize(df)
    return {"mesaj": f"{sinif} dersleri havuza aktarıldı."}

@app.post("/api/program/tumunu-havuza-at")
def tumunu_havuza_at():
    prog = prog_normalize(state.get("uretilen_program"))
    kalan = list(state.get("uretilen_kalan") or [])
    if prog is None:
        raise HTTPException(404, "Program bulunamadı.")
    
    islenen = set()
    for g in range(get_max_gun()):
        for s in range(1, get_max_saat() + 1):
            if s not in prog.get(g, {}):
                continue
            for snf, cell in list(prog[g][s].items()):
                if cell and (g, s, snf) not in islenen:
                    m_ders = _to_lower_tr(cell.get("ders", ""))
                    m_ogr = _norm_ogr(cell.get("ogretmen", ""))
                    ortak_siniflar = [
                        k for k, v in prog[g][s].items()
                        if v and _to_lower_tr(v.get("ders", "")) == m_ders and _norm_ogr(v.get("ogretmen", "")) == m_ogr
                    ]
                    sinif_str = " + ".join(sorted(ortak_siniflar, key=_dogal_sirala))
                    kalan.append({
                        "sinif": sinif_str,
                        "ders": cell["ders"],
                        "ogretmen": cell["ogretmen"],
                        "blok": 1
                    })
                    for osnf in ortak_siniflar:
                        islenen.add((g, s, osnf))
                        prog[g][s][osnf] = None

    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})
    state.havuzu_sirala()
    df = state.get("df_ders_yuku")
    if df is not None:
        _prog_senkronize(df)
    return {"mesaj": "Tüm dersler havuza aktarıldı."}

@app.post("/api/program/havuz-yerlestir")
def havuz_yerlestir(istek: HavuzYerlestir):
    prog = prog_normalize(state.get("uretilen_program"))
    kalan = state.get("uretilen_kalan") or []
    
    if prog is None:
        raise HTTPException(404, "Program bulunamadı.")
        
    if istek.kalan_idx < 0 or istek.kalan_idx >= len(kalan):
        raise HTTPException(400, "Geçersiz havuz indeksi.")
        
    # 1. Havuzdan ilgili ders kartını çekiyoruz
    kart = kalan.pop(istek.kalan_idx)
    sinif_str = kart.get("sinif", "")
    ders_adi = kart.get("ders", "")
    ogretmen_adi = kart.get("ogretmen", "")
    
    # Sınıf bilgisini parçalıyoruz (Örn: "5-A + 5-B" gibi birleşik sınıflar için)
    siniflar = [s.strip() for s in sinif_str.split("+") if s.strip()]
    
    prog.setdefault(istek.gun, {}).setdefault(istek.saat, {})
    
    # 2. Eğer yerleştirilecek hedef hücrelerde halihazırda başka dersler varsa,
    # veri kaybını önlemek için onları güvenli bir şekilde havuza geri fırlatıyoruz.
    for snf in siniflar:
        mevcut = prog[istek.gun][istek.saat].get(snf)
        if mevcut:
            kalan.append({
                "sinif": snf,
                "ders": mevcut["ders"],
                "ogretmen": mevcut["ogretmen"],
                "blok": 1
            })
            
    # 3. Havuzdan aldığımız yeni dersi hedef hücrelere yazıyoruz
    for snf in siniflar:
        prog[istek.gun][istek.saat][snf] = {
            "ders": ders_adi,
            "ogretmen": ogretmen_adi
        }
        
    # 4. State güncellemelerini yapıp havuzu yeniden sıralıyoruz
    state.update({"uretilen_program": prog, "uretilen_kalan": kalan})
    state.havuzu_sirala()
    
    # Excel yükü ile senkronizasyonu tetikliyoruz
    df = state.get("df_ders_yuku")
    if df is not None:
        _prog_senkronize(df)
        
    return {
        "mesaj": "Ders havuzdan başarıyla yerleştirildi.",
        "program": state.get("uretilen_program"),
        "kalan": state.get("uretilen_kalan")
    }
@app.post("/api/program/slot-yaz")
def slot_yaz(istek: SlotYazIstek):
    prog = prog_normalize(state.get("uretilen_program"))
    if prog is None:
        raise HTTPException(404, "Program yok.")

    for item in istek.silimler:
        if item.gun in prog and item.saat in prog[item.gun]:
            mevcut = prog[item.gun][item.saat].get(item.sinif)
            if mevcut:
                m_ders = _to_lower_tr(mevcut.get("ders", ""))
                m_ogr = _norm_ogr(mevcut.get("ogretmen", ""))
                ortak_siniflar = [k for k, v in prog[item.gun][item.saat].items() 
                                  if v and _to_lower_tr(v.get("ders", "")) == m_ders and _norm_ogr(v.get("ogretmen", "")) == m_ogr]
                for osnf in ortak_siniflar:
                    prog[item.gun][item.saat][osnf] = None
            else:
                prog[item.gun][item.saat][item.sinif] = None

    for item in istek.yazimlar:
        prog.setdefault(item.gun, {}).setdefault(item.saat, {})
        siniflar = [s.strip() for s in item.sinif.split("+") if s.strip()]
        for snf in siniflar:
            prog[item.gun][item.saat][snf] = {
                "ders": item.ders,
                "ogretmen": item.ogretmen
            }

    state.set("uretilen_program", prog)
    
    df = state.get("df_ders_yuku")
    if df is not None:
        _prog_senkronize(df)
        
    return {"mesaj": "Güncellendi.", "program": state.get("uretilen_program"), "kalan": state.get("uretilen_kalan")}


@app.post("/api/program/ders-guncelle")
def program_ders_guncelle(istek: ProgramDersGuncelleIstek):
    prog = prog_normalize(state.get("uretilen_program"))
    if not prog: 
        raise HTTPException(404, "Program bulunamadı.")
    
    ders_adi = istek.yeni_ders if istek.yeni_ders else istek.ders
    ogr_adi = istek.yeni_ogretmen if istek.yeni_ogretmen else istek.ogretmen
    
    if istek.gun in prog and istek.saat in prog[istek.gun]:
        h = prog[istek.gun][istek.saat].get(istek.sinif)
        if h:
            if ders_adi is not None:
                h["ders"] = str(ders_adi).strip()
            if ogr_adi is not None:
                h["ogretmen"] = str(ogr_adi).strip()
    
    state.set("uretilen_program", prog)
    return {"mesaj": "Ders hücrede başarıyla güncellendi."}

# ════════════════════════════════════════════════════════════════════════════
#  KARMA (YENİDEN YERLEŞTİRME)
# ════════════════════════════════════════════════════════════════════════════

@app.post("/api/program/karma")
def karma_yap(istek: KarmaIstek):
    s = state.snapshot()
    prog_original = prog_normalize(s.get("uretilen_program"))
    if prog_original is None:
        raise HTTPException(404, "Önce program üretilmeli.")
    if s["motor_calisiyor"]:
        raise HTTPException(409, "Motor zaten çalışıyor, lütfen bekleyin.")

    hedef_siniflar = set(istek.siniflar)
    sinif_listesi  = list(s["sinif_listesi"])
    ogretmen_listesi = list(s["ogretmen_listesi"])
    ogr_izin = dict(s["ogretmen_izinleri"])
    snf_izin = dict(s["sinif_izinleri"])
    kilitli_manual = state.get("kilitli_slotlar") or {}
    df = s["df_ders_yuku"].copy()
    kalan_original = list(s.get("uretilen_kalan") or [])

    state.set("karma_oncesi_snapshot", {
        "program": copy.deepcopy(prog_original),
        "kalan": copy.deepcopy(kalan_original)
    })

    cols = list(df.columns)
    c_snf  = _sutun_bul(cols, ["sinif", "class"])
    c_ders = _sutun_bul(cols, ["ders", "lesson"])
    c_ogr  = _sutun_bul(cols, ["ogretmen", "teacher"])
    c_saat = _sutun_bul(cols, ["saat", "hour"])

    df_copy = df.copy()
    kilitli_liste = []
    islenen_mufredat = set()

    for v in kilitli_manual.values():
        try:
            k_ders = _to_lower_tr(v.get("ders", ""))
            k_isim = _to_lower_tr(v.get("isim", ""))
            k_tip  = v.get("tip")
            k_ogr  = v.get("kilitliOgr")
            g, st  = int(v["gun"]), int(v["saat"])
            if not k_ders or not k_isim:
                continue

            paydas_siniflar = set()
            paydas_hocalar  = set()
            bulunan_idx = -1
            bulunan_ogr = None

            for idx2, row in df_copy.iterrows():
                db_ders = _to_lower_tr(row[c_ders])
                db_snf_list = [_to_lower_tr(sn) for sn in str(row[c_snf]).split('+') if sn.strip()]
                db_ogr_list = [x.strip() for x in _norm_ogr(row[c_ogr]).split('+') if x.strip()]
                mevcut = int(float(str(row[c_saat])))

                ders_es = (k_ders in db_ders) or (db_ders in k_ders)
                kaynak_es = (k_isim in db_snf_list) if k_tip == "sinif" else (k_isim in db_ogr_list)

                if ders_es and kaynak_es and mevcut > 0:
                    if k_ogr and k_ogr.strip() and k_ogr != "None":
                        k_ogr_norm_list = [x.strip() for x in _norm_ogr(k_ogr).split('+') if x.strip()]
                        if not all(h in db_ogr_list for h in k_ogr_norm_list):
                            continue
                    paydas_siniflar.update([sn.strip() for sn in str(row[c_snf]).split('+')])
                    paydas_hocalar.update([og.strip() for og in str(row[c_ogr]).split('+')])
                    bulunan_idx = idx2
                    bulunan_ogr = row[c_ogr]
                    break

            hedefler = []
            if bulunan_idx != -1:
                for sn in paydas_siniflar:
                    hedefler.append(("sinif", sn))
                for og in paydas_hocalar:
                    hedefler.append(("ogretmen", og))
            else:
                hedefler.append((k_tip, v.get("isim")))

            for tur, ad in hedefler:
                kilitli_liste.append({
                    "kaynak_turu": tur,
                    "kaynak_adi": ad,
                    "gun": g,
                    "saat": st,
                    "ders": v.get("ders"),
                    "ogretmen": k_ogr or (bulunan_ogr if bulunan_ogr else None)
                })

            if bulunan_idx != -1 and (bulunan_idx, g, st) not in islenen_mufredat:
                satir_siniflar = [x.strip() for x in str(df_copy.at[bulunan_idx, c_snf]).split('+')]
                if any(sn in hedef_siniflar for sn in satir_siniflar):
                    df_copy.at[bulunan_idx, c_saat] = max(0, int(float(str(df_copy.at[bulunan_idx, c_saat]))) - 1)
                    islenen_mufredat.add((bulunan_idx, g, st))
        except Exception as e:
            print(f"[KARMA KİLİT UYARI] {e}")

    for g in range(get_max_gun()):
        for st in range(1, get_max_saat() + 1):
            if g not in prog_original or st not in prog_original[g]:
                continue
            for snf, ders in prog_original[g][st].items():
                if snf in hedef_siniflar:
                    continue
                if ders is None:
                    continue
                kilitli_liste.append({
                    "kaynak_turu": "sinif",
                    "kaynak_adi": snf,
                    "gun": g,
                    "saat": st,
                    "ders": ders.get("ders"),
                    "ogretmen": ders.get("ogretmen")
                })
                for o in ders.get("ogretmen", "").split("+"):
                    o = o.strip()
                    if o:
                        kilitli_liste.append({
                            "kaynak_turu": "ogretmen",
                            "kaynak_adi": o,
                            "gun": g,
                            "saat": st,
                            "ders": ders.get("ders"),
                            "ogretmen": o
                        })

    gorulen = set()
    benzersiz = []
    for k in kilitli_liste:
        tup = (k["kaynak_turu"], k["kaynak_adi"], k["gun"], k["saat"])
        if tup not in gorulen:
            gorulen.add(tup)
            benzersiz.append(k)
    kilitli_liste = benzersiz

    kalan_sayaci = {}
    kalan_diger = []

    for d in kalan_original:
        kart_siniflar = [x.strip() for x in d.get("sinif", "").split("+") if x.strip()]
        if any(sn in hedef_siniflar for sn in kart_siniflar):
            d_snf = " + ".join(sorted(kart_siniflar))
            d_ders = str(d.get("ders", "")).strip()
            d_ogr = str(d.get("ogretmen", "")).strip()
            d_blok = int(d.get("blok", 1))
            key = (d_snf, d_ders, d_ogr)
            kalan_sayaci[key] = kalan_sayaci.get(key, 0) + d_blok
        else:
            kalan_diger.append(d)

    secili_satirlar = []
    for _, row in df_copy.iterrows():
        satir_siniflar = [x.strip() for x in str(row[c_snf]).split('+') if x.strip()]
        if not any(sn in hedef_siniflar for sn in satir_siniflar):
            continue
        
        saat = int(float(str(row[c_saat])))
        if saat <= 0:
            continue

        r_snf = " + ".join(sorted(satir_siniflar))
        r_ders = str(row[c_ders]).strip()
        r_ogr = str(row[c_ogr]).strip()
        key = (r_snf, r_ders, r_ogr)

        pool_saat = min(kalan_sayaci.get(key, 0), saat)
        assigned_saat = saat - pool_saat

        if assigned_saat > 0:
            secili_satirlar.append({
                c_snf: row[c_snf],
                c_ders: row[c_ders],
                c_ogr: row[c_ogr],
                c_saat: assigned_saat
            })

        for _ in range(pool_saat):
            secili_satirlar.append({
                c_snf: row[c_snf],
                c_ders: row[c_ders],
                c_ogr: row[c_ogr],
                c_saat: 1
            })

        if key in kalan_sayaci:
            kalan_sayaci[key] -= pool_saat
            if kalan_sayaci[key] <= 0:
                del kalan_sayaci[key]

    for (r_snf, r_ders, r_ogr), p_saat in kalan_sayaci.items():
        if p_saat > 0:
            for _ in range(p_saat):
                secili_satirlar.append({
                    c_snf: r_snf,
                    c_ders: r_ders,
                    c_ogr: r_ogr,
                    c_saat: 1
                })

    if not secili_satirlar:
        raise HTTPException(400, "Seçili sınıflara ait yerleştirilecek ders kalmadı.")

    df_secili = pd.DataFrame(secili_satirlar, columns=[c_snf, c_ders, c_ogr, c_saat])

    sure = max(5, min(600, istek.sure_saniye or 120))
    original_kontrol = engine_mod.EnginePreCheck.kontrol_et
    engine_mod.EnginePreCheck.kontrol_et = lambda self: (setattr(self, 'hatalar', []) or True)

    try:
        sonuc = program_hesapla(
            df_secili,
            sinif_listesi,
            ogretmen_listesi,
            ogr_izin, snf_izin,
            sure_saniye=sure,
            kilitli_saatler=kilitli_liste,
            gelismis_ayarlar=s.get("gelismis_ayarlar", {})
        )
    finally:
        engine_mod.EnginePreCheck.kontrol_et = original_kontrol

    if sonuc["durum"] == "TAMAM":
        yeni_prog = prog_normalize(sonuc["program"])

        birlesik_prog = copy.deepcopy(prog_original)
        for g in range(get_max_gun()):
            for st in range(1, get_max_saat() + 1):
                if g in birlesik_prog and st in birlesik_prog[g]:
                    for snf in list(birlesik_prog[g][st].keys()):
                        if snf in hedef_siniflar:
                            birlesik_prog[g][st][snf] = None

        for g in range(get_max_gun()):
            for st in range(1, get_max_saat() + 1):
                if g in yeni_prog and st in yeni_prog[g]:
                    for snf, ders in yeni_prog[g][st].items():
                        if ders is not None:
                            birlesik_prog.setdefault(g, {}).setdefault(st, {})[snf] = ders

        for v in kilitli_manual.values():
            try:
                g, st = int(v["gun"]), int(v["saat"])
                k_snf = v.get("isim")
                k_ders = v.get("ders")
                k_ogr = v.get("kilitliOgr")
                
                hedef_snf_list = []
                if v.get("tip") == "sinif":
                    hedef_snf_list = [k_snf]
                    for snf in sinif_listesi:
                        if k_snf in snf.split("+"):
                            hedef_snf_list.append(snf)
                    hedef_snf_list = list(set(hedef_snf_list))
                else:  
                    if g in prog_original and st in prog_original[g]:
                        for snf, d in prog_original[g][st].items():
                            if d and k_ogr and k_ogr in d.get("ogretmen", ""):
                                hedef_snf_list.append(snf)
                    if not hedef_snf_list:
                        hedef_snf_list = [k_snf]

                for snf in hedef_snf_list:
                    orj_ders = None
                    if g in prog_original and st in prog_original[g]:
                        orj_ders = prog_original[g][st].get(snf)
                    if orj_ders:
                        birlesik_prog.setdefault(g, {}).setdefault(st, {})[snf] = {
                            "ders": orj_ders["ders"],
                            "ogretmen": orj_ders["ogretmen"]
                        }
                    else:
                        birlesik_prog.setdefault(g, {}).setdefault(st, {})[snf] = {
                            "ders": k_ders,
                            "ogretmen": k_ogr or "İsimsiz"
                        }
            except Exception as e:
                print(f"[KARMA KİLİT YAZMA UYARI] {e}")

        for g in range(get_max_gun()):
            if g in birlesik_prog:
                for st in range(1, get_max_saat() + 1):
                    if st in birlesik_prog[g]:
                        birlesik_prog[g][st] = {k: v for k, v in birlesik_prog[g][st].items() if v is not None}

        yeni_kalan = list(sonuc.get("kalan", [])) + kalan_diger

        state.update({
            "uretilen_program": birlesik_prog,
            "uretilen_kalan": yeni_kalan,
            "motor_calisiyor": False,
            "son_motor_sonuc": "TAMAM",
            "hata_mesaji": sonuc.get("hata_mesaji", ""),
        })
        state.havuzu_sirala()
        return {"mesaj": "Karma başarılı.", "program": birlesik_prog, "kalan": yeni_kalan, "snapshot_var": True}
    else:
        state.update({
            "uretilen_program": copy.deepcopy(prog_original),
            "uretilen_kalan": copy.deepcopy(kalan_original),
            "motor_calisiyor": False,
            "son_motor_sonuc": sonuc["durum"],
            "hata_mesaji": sonuc.get("hata_mesaji", ""),
        })
        raise HTTPException(500, f"Karma başarısız: {sonuc.get('hata_mesaji','Bilinmeyen hata')}")

@app.post("/api/program/karma-geri-al")
def karma_geri_al():
    snap = state.get("karma_oncesi_snapshot")
    if not snap:
        raise HTTPException(404, "Geri alınacak bir yedek bulunamadı.")
    state.update({
        "uretilen_program": copy.deepcopy(snap["program"]),
        "uretilen_kalan": copy.deepcopy(snap["kalan"])
    })
    state.set("karma_oncesi_snapshot", None)
    return {
        "mesaj": "İşlem geri alındı. Önceki programa dönüldü.",
        "program": snap["program"],
        "kalan": snap["kalan"]
    }

# ════════════════════════════════════════════════════════════════════════════
#  EXCEL ÇIKTISI VE AYARLARI
# ════════════════════════════════════════════════════════════════════════════

EXCEL_AYARLARI_FILE = "excel_ayarlari.json"

def _get_excel_ayarlari() -> dict:
    varsayilan = {
        "tekli_ders_f_size": 11,
        "tekli_ogr_f_size": 10,
        "tekli_snf_f_size": 11,
        "dortlu_ders_f_size": 8,
        "dortlu_ogr_f_size": 8,
        "dortlu_snf_f_size": 8,
        "tekli_row_h": 65,
        "dortlu_row_h": 30,
        "tekli_sutun_genislik": 26,
        "dortlu_sutun_genislik": 12,
        "gun_f_size": 9,
        "saat_f_size": 7,
        "gun_row_h": 15,
        "saat_col_w": 12
    }
    if os.path.exists(EXCEL_AYARLARI_FILE):
        try:
            with open(EXCEL_AYARLARI_FILE, "r", encoding="utf-8") as f:
                kayitli = json.load(f)
                varsayilan.update(kayitli)
        except Exception:
            pass
    return varsayilan

@app.get("/api/ayarlar/excel")
def excel_ayarlari_getir():
    return _get_excel_ayarlari()

@app.post("/api/ayarlar/excel")
def excel_ayarlari_kaydet(ayarlar: ExcelAyarlari):
    try:
        with open(EXCEL_AYARLARI_FILE, "w", encoding="utf-8") as f:
            json.dump(ayarlar.dict(), f, ensure_ascii=False, indent=4)
        return {"mesaj": "Excel ayarları başarıyla kaydedildi.", "ayarlar": ayarlar.dict()}
    except Exception as e:
        raise HTTPException(500, f"Ayar kaydedilirken hata oluştu: {e}")

@app.post("/api/ayarlar/excel/varsayilan")
def excel_ayarlari_sifirla():
    if os.path.exists(EXCEL_AYARLARI_FILE):
        try:
            os.remove(EXCEL_AYARLARI_FILE)
        except:
            pass
    return {"mesaj": "Varsayılan ayarlara dönüldü.", "ayarlar": _get_excel_ayarlari()}

@app.post("/api/excel-ciktisi")
def excel_ciktisi(istek: ExcelIstek):
    s = state.snapshot()
    if s["uretilen_program"] is None:
        raise HTTPException(404, "Önce program oluşturun.")
    state.saatleri_hesapla()
    
    ayarlar = _get_excel_ayarlari()
    
    dosya_bytes = excel_uret(
        program               = s["uretilen_program"],
        sinif_listesi         = s["sinif_listesi"],
        ogretmen_listesi      = s["ogretmen_listesi"],
        ogretmen_izinleri     = s["ogretmen_izinleri"],
        aktif_saat_etiketleri = state.get("aktif_saat_etiketleri"),
        siyah_beyaz           = istek.siyah_beyaz,
        ayarlar               = ayarlar
    )
    return StreamingResponse(
        io.BytesIO(dosya_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=akkan_program.xlsx"},
    )

# ════════════════════════════════════════════════════════════════════════════
#  PROJE KAYDET / YÜKLE
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/proje/kaydet")
def proje_kaydet():
    s = state.snapshot()
    df = s["df_ders_yuku"]
    df_json = []
    if df is not None:
        for _, row in df.iterrows():
            try:
                df_json.append({
                    str(col): (int(val) if 'saat' in str(col).lower() else str(val) if str(val) != 'nan' else '')
                    for col, val in row.items()
                })
            except Exception:
                pass
    veri = {
        "versiyon":          3,
        "ogretmen_listesi":  s["ogretmen_listesi"],
        "sinif_listesi":     s["sinif_listesi"],
        "ders_listesi":      s["ders_listesi"],
        "ogretmen_izinleri": {k: [list(t) for t in v] for k, v in s["ogretmen_izinleri"].items()},
        "sinif_izinleri":    {k: [list(t) for t in v] for k, v in s["sinif_izinleri"].items()},
        "zaman_ayarlari":    {k: str(v) if not isinstance(v, dict) else v for k, v in s["zaman_ayarlari"].items()},
        "df_ders_yuku":      df_json,
        "uretilen_program":  s["uretilen_program"],
        "uretilen_kalan":    s["uretilen_kalan"],
        "kilitli_slotlar":   state.get("kilitli_slotlar") or {},
        "gelismis_ayarlar":  s.get("gelismis_ayarlar") or {},
    }
    icerik = json.dumps(veri, ensure_ascii=False).encode('utf-8')
    return StreamingResponse(io.BytesIO(icerik), media_type="application/octet-stream",
                             headers={"Content-Disposition": "attachment; filename=proje.enderun"})

@app.post("/api/proje/yukle")
async def proje_yukle(dosya: UploadFile = File(...)):
    icerik = await dosya.read()
    veri = None
    try:
        veri = json.loads(icerik.decode("utf-8"))
    except:
        try:
            veri = pickle.loads(icerik)
        except:
            pass

    if veri is None:
        raise HTTPException(400, "Dosya okunamadı.")

    try:
        df_ham_raw = veri.get("df_ders_yuku", None)
        df_ham = None
        if df_ham_raw is not None:
            rows = []
            for r in df_ham_raw:
                try:
                    vals = list(r.values()) if isinstance(r, dict) else list(r)
                    if len(vals) < 4:
                        continue
                    rows.append({
                        "Sınıf": str(vals[0]) if len(vals) > 0 else "",
                        "Ders Adı": str(vals[1]) if len(vals) > 1 else "",
                        "Öğretmen": str(vals[2]) if len(vals) > 2 else "",
                        "Haftalık Saat": vals[3] if len(vals) > 3 else 0,
                    })
                except Exception:
                    continue
            df_ham = pd.DataFrame(rows)

        z = veri.get("zaman_ayarlari", {})
        state.update({
            "ogretmen_listesi": veri.get("ogretmen_listesi", []),
            "sinif_listesi": veri.get("sinif_listesi", []),
            "ders_listesi": veri.get("ders_listesi", []),
            "ogretmen_izinleri": veri.get("ogretmen_izinleri", {}),
            "sinif_izinleri": veri.get("sinif_izinleri", {}),
            "df_ders_yuku": df_ham,
            "uretilen_program": veri.get("uretilen_program", None),
            "uretilen_kalan": veri.get("uretilen_kalan", []),
            "kilitli_slotlar": veri.get("kilitli_slotlar", {}),
            "gelismis_ayarlar": veri.get("gelismis_ayarlar", {}),
            "zaman_ayarlari": {
                "baslama": str(z.get("baslama", "09:15")),
                "ders_dk": int(z.get("ders_dk", 40)),
                "teneffus_dk": int(z.get("teneffus_dk", 10)),
                "ogle_sonrasi_ders": int(z.get("ogle_sonrasi_ders", 5)),
                "ogle_dk": int(z.get("ogle_dk", 50)),
                "ozel_saatler": z.get("ozel_saatler", {}),
            },
        })
        state.listeleri_guncelle()
        state.saatleri_hesapla()
        state.havuzu_sirala()
        return {"mesaj": "Proje başarıyla yüklendi."}
    except Exception as e:
        raise HTTPException(500, f"Hata: {str(e)}")

# ════════════════════════════════════════════════════════════════════════════
#  STATİK DOSYA SUNUMU
# ════════════════════════════════════════════════════════════════════════════

app.mount("/", StaticFiles(directory="frontend", html=True), name="static")