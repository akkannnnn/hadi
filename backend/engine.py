# backend/engine.py
# ════════════════════════════════════════════════════════════════════════════
#  Ders Programı Çizelgeleme Motoru — v9.0.1
#  Greedy Warm-Start + 3-Aşamalı CP-SAT + LNS Swap Repair
# ════════════════════════════════════════════════════════════════════════════
"""
v9.0.1 — Havuz/Program Uyum Düzeltmesi

Mimari:
    Aşama 0 (Greedy)       : Açgözlü heuristic ile ilk çözüm → warm-start.
    Aşama 1 (Hard)         : CP-SAT, presence zorunlu (tüm ders yerleşmeli).
    Aşama 2 (Soft)         : CP-SAT, presence opsiyonel (maks. yerleşim).
    Aşama 3 (Repair + LNS) : Havuzdakiler için LNS komşuluk çözümleri.

Kısıtlar (HİÇBİRİ gereksiz yere esnetilmez):
    1. NoOverlap (öğretmen + sınıf)
    2. Gün Bütünlüğü — hour değişkenin aralığı ile OTOMATİK sağlanır.
    3. Simetri Kırma.
    4. Gün Yayılımı — aynı (sınıf, ders) farklı güne; imkansızsa GAP ile aynı gün.
    5. Öğretmen-Sınıf Günü — aynı (ogr, snf) aynı günde max 1 blok.
    6. Ardışıklık Yasağı — aynı dersin aynı gündeki blokları arasında en az 1 saat.

v9.0 → v9.0.1 değişikliği:
    • Havuz (kalan) kartları artık HER BİRİ 1 SAATLİK birim olarak üretilir.
      Yerleşemeyen bir 2 saatlik blok, 2 ayrı 1 saatlik kart olarak havuza
      eklenir. Böylece programdaki 1 saatlik boş hücreler ile havuzdaki
      kartlar bire bir eşleşir (eski v9.0'daki "havuzda 4 saat, programda
      dağınık 1 saatlik boşluklar" uyumsuzluğu giderildi).
    • _task_to_dict: saat=1 döner.
    • _prog_olustur: yerleşemeyen her task için range(t.blok) kadar kart.
    • _prog_olustur: (sinif, ders, ogretmen) bazlı birleştirme KALDIRILDI.
    • _kapasite_uyari_kontrol: 1 saatlik kartlardan toplam saat hesaplanır;
      her grup için yalnızca bir uyarı yayınlanır.
    • INFEASIBLE / HATA / PRE_CHECK_FAILED dönüşlerinde kalan da 1 saatlik
      birimler halinde üretilir.

v7.1 / v8.0 → v9.0'da kalıcı iyileştirmeler:
    • AddDivisionEquality / AddModuloEquality KALDIRILDI → manuel day/hour ayrıştırma.
    • Kısıt 2 (gün bütünlüğü) hour değişkeninin aralığı sayesinde ücretsiz.
    • Hard modda Kısıt 4 ve 5 için AddAllDifferent optimizasyonu.
    • Greedy warm-start (v8.0'dan alındı, temizlenmiş hali).
    • LNS Swap Repair (komşu task'leri serbest bırakma).
    • Kısıt 6 "aynı ders 2+2 yasağı" matematiksel imkansızlık halinde esnetilir.
    • Modül seviyesinde logging.basicConfig YOK (import eden uygulamayı ezmez).
    • AddDivisionEquality yerine manuel ayrıştırma; performans kazancı büyük.
    • Türkçe karakterli değişken adları kaldırıldı.
    • `except Exception: pass` yerine log.debug ile şeffaf hata yönetimi.
    • _df_normalize eksik kolon bulursa SESSİZ DEĞİL — açık hata döner.
    • Objective formülü: soft modda presence için dev baz ağırlık → max yerleşim.
"""

from __future__ import annotations

import logging
import os
import random
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# ────────────────────────────────────────────────────────────────────────────
#  LOGGING
#  Modül seviyesinde basicConfig YOK — import eden uygulamanın loglarını ezmez.
# ────────────────────────────────────────────────────────────────────────────
log = logging.getLogger("engine")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


# ────────────────────────────────────────────────────────────────────────────
#  OR-TOOLS
# ────────────────────────────────────────────────────────────────────────────
try:
    from ortools.sat.python import cp_model
    ORTOOLS_VAR = True
except ImportError:
    cp_model = None  # type: ignore[assignment]
    ORTOOLS_VAR = False


# ────────────────────────────────────────────────────────────────────────────
#  SABİTLER / KONFİG
# ────────────────────────────────────────────────────────────────────────────
GUN_SAYISI   = 5
SAAT_SAYISI  = 8
HORIZON      = GUN_SAYISI * SAAT_SAYISI  # 40

# ── Süre dağılımı (toplam 1.00) ──
SURE_ORAN_HARD     = 0.50
SURE_ORAN_SOFT     = 0.35
SURE_ORAN_REPAIR   = 0.15
SURE_HARD_MIN      = 30.0
SURE_REPAIR_LNS_PAY = 0.40    # Repair süresinin ne kadarı LNS'ye ayrılsın
LNS_MAX_DENEME      = 12      # En fazla kaç LNS tur dönülsün

# ── Objective ağırlıkları ──
W_BASE           = 1
W_YOGUNLUK_MULT  = 100
W_SAAT_MULT      = 5
W_BLOK_MULT      = 3
# Soft modda presence=1 kazancı diğer tüm terimlerden büyük katsayıyla çarpılır
# (lexicographic değil ama pratikte "önce yerleşim, sonra kalite").
W_PRESENCE_MULT  = 10_000

# ── Greedy hafta-yayılım eşikleri ──
YOGUNLUK_ESIK_YUKSEK = 0.60
YOGUNLUK_ESIK_ORTA   = 0.30

# ── Solver parametreleri ──
SOLVER_WORKER_MAX = 8


def varsayilan_blok_bolumleme(saat: int) -> List[int]:
    """
    Ders saatini 2'li bloklara böler ve varyasyon sağlamak için karıştırır.
    Örn: 5 → [2,2,1], [1,2,2] veya [2,1,2] şeklinde rastgele döner.
    """
    bloklar: List[int] = []
    kalan = saat
    while kalan >= 2:
        bloklar.append(2)
        kalan -= 2
    if kalan == 1:
        bloklar.append(1)
        
    # <--- YENİ EKLENEN KISIM: Blokları karıştır --->
    import random
    random.shuffle(bloklar)
    
    return bloklar


# ════════════════════════════════════════════════════════════════════════════
#  VERİ YAPILARI
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Task:
    """Tek bir ders bloğunu temsil eder (1 veya 2 saat)."""
    id          : int
    siniflar    : List[str]
    ogretmenler : List[str]
    ders        : str
    blok        : int        # bu görevin ders süresi (1 veya 2)
    blok_idx    : int        # aynı (sınıf+ders) içindeki blok indexi
    toplam_blok : int        # aynı (sınıf+ders) için kaç blok var
    saat        : int        # dersin haftalık toplam saati (info amaçlı)

    def kaynak_anahtarlari(self) -> List[str]:
        """NoOverlap için bu görevin paylaştığı tüm kaynak anahtarları."""
        return [f"O_{o}" for o in self.ogretmenler] + \
               [f"C_{c}" for c in self.siniflar]


@dataclass
class KilitliSaat:
    """Önceden belirlenmiş (dondurulmuş) slot."""
    kaynak_turu : str                       # "ogretmen" | "sinif"
    kaynak_adi  : str
    gun         : int                       # 0..GUN_SAYISI-1
    saat        : int                       # 1..SAAT_SAYISI
    ders        : Optional[str] = None


@dataclass
class PhaseResult:
    """Bir aşamanın tamamlanmış sonucu (taşıma kolaylığı için)."""
    status       : int
    sure         : float
    durum_str    : str
    solver       : Any = None
    builder      : Any = None
    yerlesen     : int = 0
    yerlesmeyen  : Set[int] = field(default_factory=set)


# ════════════════════════════════════════════════════════════════════════════
#  YARDIMCI — METİN / SLUG
# ════════════════════════════════════════════════════════════════════════════

def _slug(s: str) -> str:
    """CP-SAT değişken adları için güvenli karakter."""
    out = []
    for ch in str(s):
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def _norm_tr(s: str) -> str:
    """Türkçe karakterleri ascii'ye, küçük harfe."""
    t = str(s).lower().strip()
    for a, b in (("ı","i"), ("ş","s"), ("ğ","g"),
                 ("ö","o"), ("ü","u"), ("ç","c"), ("İ","i")):
        t = t.replace(a, b)
    return t


# ════════════════════════════════════════════════════════════════════════════
#  MODÜL — ENGINE PRE-CHECK
# ════════════════════════════════════════════════════════════════════════════

class EnginePreCheck:
    """
    Solver'a girmeden önce zorunlu (matematiksel) kısıt ihlallerini tespit eder.

    Kontrol edilen durumlar:
      1. Öğretmen toplam kapasite (yük ≤ müsait slot)
      2. Sınıf toplam kapasite
      3. Kilit kendini çakıştırıyor mu
      4. Kısıt 5 gün yetersizliği (aynı ogr+snf için yeterli ortak gün var mı)
    """

    def __init__(self, tasks, ogretmen_izinleri, sinif_izinleri, kilitli_saatler):
        self.tasks             = tasks
        self.ogretmen_izinleri = ogretmen_izinleri
        self.sinif_izinleri    = sinif_izinleri
        self.kilitli_saatler   = kilitli_saatler
        self.hatalar: List[str] = []
        # Hızlı lookup için önden set'ler kurulur
        self._lock_sets: Dict[Tuple[str, str], Set[int]] = defaultdict(set)
        for k in self.kilitli_saatler:
            if not k.ders:
                t_idx = k.gun * SAAT_SAYISI + (k.saat - 1)
                if 0 <= t_idx < HORIZON:
                    self._lock_sets[(k.kaynak_turu, k.kaynak_adi)].add(t_idx)

    def _kapali_slotlar(self, adi: str, izinler: Dict, turu: str) -> Set[int]:
        s: Set[int] = set()
        for (saat_1, gun_0) in izinler.get(adi, []):
            t_idx = gun_0 * SAAT_SAYISI + (saat_1 - 1)
            if 0 <= t_idx < HORIZON:
                s.add(t_idx)
        s |= self._lock_sets.get((turu, adi), set())
        for k in self.kilitli_saatler:
            if k.kaynak_turu == turu and k.kaynak_adi == adi and k.ders:
                t_idx = k.gun * SAAT_SAYISI + (k.saat - 1)
                s.discard(t_idx)
        return s

    def _bos_saatler_gun(self, adi: str, izinler: Dict, turu: str, gun: int) -> List[int]:
        """O günün 1..SAAT_SAYISI aralığında açık (müsait) saatlerin listesi."""
        kapali_saatler: Set[int] = set()
        for (saat_1, gun_0) in izinler.get(adi, []):
            if gun_0 == gun:
                kapali_saatler.add(saat_1)
        for t_idx in self._lock_sets.get((turu, adi), set()):
            g = t_idx // SAAT_SAYISI
            if g == gun:
                kapali_saatler.add((t_idx % SAAT_SAYISI) + 1)
        return [s for s in range(1, SAAT_SAYISI + 1) if s not in kapali_saatler]

    def _ardisik_bos_var_mi(self, bos: List[int], gereken: int) -> bool:
        if gereken <= 1:
            return len(bos) >= 1
        for i in range(len(bos) - gereken + 1):
            ardisik = True
            for k in range(gereken - 1):
                if bos[i + k + 1] != bos[i + k] + 1:
                    ardisik = False
                    break
            if ardisik:
                return True
        return False

    def kontrol_et(self) -> bool:
        self.hatalar = []

        # 1. Öğretmen toplam kapasite
        ogr_yuk: Dict[str, int] = defaultdict(int)
        for t in self.tasks:
            for o in t.ogretmenler:
                ogr_yuk[o] += t.blok
        for ogr, yuk in sorted(ogr_yuk.items()):
            kapali = len(self._kapali_slotlar(ogr, self.ogretmen_izinleri, "ogretmen"))
            musait = HORIZON - kapali
            if yuk > musait:
                self.hatalar.append(
                    f"[KAPASİTE AŞIMI] Öğretmen '{ogr}': "
                    f"Yük={yuk} blok, Müsait={musait} slot. Fark: +{yuk - musait}."
                )

        # 2. Sınıf toplam kapasite
        snf_yuk: Dict[str, int] = defaultdict(int)
        for t in self.tasks:
            for c in t.siniflar:
                snf_yuk[c] += t.blok
        for snf, yuk in sorted(snf_yuk.items()):
            kapali = len(self._kapali_slotlar(snf, self.sinif_izinleri, "sinif"))
            musait = HORIZON - kapali
            if yuk > musait:
                self.hatalar.append(
                    f"[KAPASİTE AŞIMI] Sınıf '{snf}': "
                    f"Yük={yuk} blok, Müsait={musait} slot. Fark: +{yuk - musait}."
                )

        # 3. Kilit kendini çakıştırıyor mu
        lock_map: Dict[Tuple, List[Tuple[int, int]]] = defaultdict(list)
        for k in self.kilitli_saatler:
            lock_map[(k.kaynak_turu, k.kaynak_adi)].append((k.gun, k.saat))
        for (turu, adi), slots in lock_map.items():
            if len(slots) != len(set(slots)):
                self.hatalar.append(
                    f"[KİLİT ÇAKIŞMASI] {turu.capitalize()} '{adi}': aynı slot birden fazla kilitli."
                )

        # 4. Kısıt 5 yetersizliği: aynı (ogr, snf) için yeterli ortak gün var mı?
        ogr_snf_blok_sayi: Dict[Tuple[str, str], int] = defaultdict(int)
        ogr_snf_max_blok : Dict[Tuple[str, str], int] = defaultdict(int)
        for t in self.tasks:
            for o in t.ogretmenler:
                for c in t.siniflar:
                    ogr_snf_blok_sayi[(o, c)] += 1
                    if t.blok > ogr_snf_max_blok[(o, c)]:
                        ogr_snf_max_blok[(o, c)] = t.blok

        for (ogr, snf), blok_sayi in sorted(ogr_snf_blok_sayi.items()):
            gerekli_blok = ogr_snf_max_blok[(ogr, snf)]
            ortak_gun = 0
            for g in range(GUN_SAYISI):
                o_bos = self._bos_saatler_gun(ogr, self.ogretmen_izinleri, "ogretmen", g)
                s_bos = self._bos_saatler_gun(snf, self.sinif_izinleri,    "sinif",    g)
                kesisim = sorted(set(o_bos) & set(s_bos))
                if self._ardisik_bos_var_mi(kesisim, gerekli_blok):
                    ortak_gun += 1
            if blok_sayi > ortak_gun:
                self.hatalar.append(
                    f"[GÜN KISITI AŞIMI] Öğretmen '{ogr}' → Sınıf '{snf}': "
                    f"{blok_sayi} blok var, ortak müsait gün={ortak_gun}. "
                    f"Aynı günde max 1 blok kuralı matematiksel olarak sağlanamaz."
                )

        if self.hatalar:
            log.warning(f"[PreCheck] {len(self.hatalar)} sorun tespit edildi:")
            for h in self.hatalar:
                log.warning(f"  ✗ {h}")
        else:
            log.info("[PreCheck] Tüm kapasite/gün denetimleri geçildi.")

        return not self.hatalar

    def rapor(self) -> str:
        if not self.hatalar:
            return ""
        return (
            "━━━ KAPASİTE / GÜN KISITI RAPORU ━━━\n"
            + "\n".join(f"  • {h}" for h in self.hatalar)
            + "\n\nÇözüm önerileri:\n"
            "  • Haftalık ders saatini azaltın.\n"
            "  • İlgili öğretmenin/sınıfın izinli saatlerini azaltın.\n"
            "  • İhtiyaç olan blok büyüklüğünü küçültün (ör. 2+2 → 1+1+1+1)."
        )


# ════════════════════════════════════════════════════════════════════════════
#  MODÜL — GREEDY WARM-START
# ════════════════════════════════════════════════════════════════════════════

class GreedyInitialSolver:
    """
    Basit açgözlü yerleştirici. Tüm zorunlu kısıtları gözeterek her task
    için bir start slotu arar. Bulduğunu AddHint ile CP-SAT'e verir.

    Önemli: Greedy'nin bulamadıklarını CP-SAT'in bulması beklenir.
    """

    def __init__(self, tasks, ogretmen_izinleri, sinif_izinleri, kilitli_saatler, gelismis_ayarlar=None):
        self.tasks             = tasks
        self.ogretmen_izinleri = ogretmen_izinleri
        self.sinif_izinleri    = sinif_izinleri
        self.kilitli_saatler   = kilitli_saatler
        self.gelismis_ayarlar  = gelismis_ayarlar or {}

        # Dolu slot setleri
        self._used_slots: Dict[str, Set[int]] = defaultdict(set)
        self._init_kaynak_doluluklari()

        # (ogr, snf, gun) — Kısıt 5 için
        self._ogr_snf_gun: Set[Tuple[str, str, int]] = set()
        # (ders, gun) → [(start, blok), ...] — Kısıt 6 için
        self._ders_gun: Dict[Tuple[str, int], List[Tuple[int, int]]] = defaultdict(list)
        # (sinif_key, ders) → [gun, ...] — Kısıt 4 için (farklı güne yayma)
        self._sinif_ders_gunler: Dict[Tuple[Tuple[str, ...], str], List[int]] = defaultdict(list)

        self.task_start: Dict[int, int] = {}

    def _init_kaynak_doluluklari(self) -> None:
        for o, izinler in self.ogretmen_izinleri.items():
            key = f"O_{o}"
            for (s1, g0) in izinler:
                t_idx = g0 * SAAT_SAYISI + (s1 - 1)
                if 0 <= t_idx < HORIZON:
                    self._used_slots[key].add(t_idx)
        for c, izinler in self.sinif_izinleri.items():
            key = f"C_{c}"
            for (s1, g0) in izinler:
                t_idx = g0 * SAAT_SAYISI + (s1 - 1)
                if 0 <= t_idx < HORIZON:
                    self._used_slots[key].add(t_idx)
        for k in self.kilitli_saatler:
            on_ek = "O" if k.kaynak_turu == "ogretmen" else "C"
            key = f"{on_ek}_{k.kaynak_adi}"
            t_idx = k.gun * SAAT_SAYISI + (k.saat - 1)
            if 0 <= t_idx < HORIZON:
                self._used_slots[key].add(t_idx)

    def _interval_bos_mu(self, task: Task, start: int) -> bool:
        for offset in range(task.blok):
            t_idx = start + offset
            for key in task.kaynak_anahtarlari():
                if t_idx in self._used_slots[key]:
                    return False
        return True

    def _gun_butunluk(self, start: int, blok: int) -> bool:
        hour = start % SAAT_SAYISI
        return hour + blok <= SAAT_SAYISI

    def _ogr_snf_gun_ok(self, task: Task, gun: int) -> bool:
        if not self.gelismis_ayarlar.get("ogr_snf_gunluk_limit", True): return True # YENİ
        for o in task.ogretmenler:
            for c in task.siniflar:
                if (o, c, gun) in self._ogr_snf_gun:
                    return False
        return True

    def _ardisiklik_ok(self, task: Task, start: int, gun: int, imkansiz: bool) -> bool:
        gap = self.gelismis_ayarlar.get("ardisiklik_boslugu", 1) # YENİ
        if imkansiz or gap <= 0: return True # YENİ
        end = start + task.blok
        for (other_start, other_blok) in self._ders_gun.get((task.ders, gun), []):
            other_end = other_start + other_blok
            # Her iki yönde de en az 'gap' kadar boşluk olmalı (YENİ MANTIK)
            if not (start >= other_end + gap or end + gap <= other_start):
                return False
        return True

    def _gun_yayilim_ok(self, task: Task, gun: int,
                        sinif_key: Tuple[str, ...], grup_blok: int) -> bool:
        if not self.gelismis_ayarlar.get("gun_yayilimi", True): return True # YENİ
        if grup_blok > GUN_SAYISI: return True
        return gun not in self._sinif_ders_gunler.get((sinif_key, task.ders), [])

    def solve(self) -> Dict[int, int]:
        # 1. ADIM: Önce kilitli dersleri tam yerlerine oturt
        for k in self.kilitli_saatler:
            if not k.ders:
                continue
            t_idx = k.gun * SAAT_SAYISI + (k.saat - 1)
            for task in self.tasks:
                if task.id in self.task_start:
                    continue
                uygun = (
                    (k.kaynak_turu == "ogretmen" and k.kaynak_adi in task.ogretmenler) or
                    (k.kaynak_turu == "sinif" and k.kaynak_adi in task.siniflar)
                )
                if uygun and task.ders == k.ders:
                    # Slotu ve kaynakları rezerve et
                    for offset in range(task.blok):
                        for key in task.kaynak_anahtarlari():
                            self._used_slots[key].add(t_idx + offset)
                    for o in task.ogretmenler:
                        for c in task.siniflar:
                            self._ogr_snf_gun.add((o, c, k.gun))
                    self._ders_gun[(task.ders, k.gun)].append((t_idx, task.blok))
                    self.task_start[task.id] = t_idx
                    break

        # 2. ADIM: Kalan serbest dersleri yerleştir (mevcut döngü)
        sinif_ders_sayi: Dict[Tuple[Tuple[str, ...], str], int] = defaultdict(int)
        for t in self.tasks:
            sinif_ders_sayi[(tuple(sorted(t.siniflar)), t.ders)] += 1

        def zorluk(t: Task) -> Tuple:
            return (-t.blok, -len(t.ogretmenler), -t.saat, random.random())
            
        sirali = sorted([t for t in self.tasks if t.id not in self.task_start], key=zorluk)

        for task in sirali:
            tid = task.id
            sinif_key = tuple(sorted(task.siniflar))
            grup_blok = sinif_ders_sayi[(sinif_key, task.ders)]
            imkansiz  = grup_blok > GUN_SAYISI

            yerlesti = False
            # Slot sıralamasını biraz karıştır: günün ortasına hafif tercih
            slot_order = list(range(HORIZON - task.blok + 1))
            random.shuffle(slot_order)
            for start in slot_order:
                if not self._gun_butunluk(start, task.blok):
                    continue
                gun = start // SAAT_SAYISI
                if not self._interval_bos_mu(task, start):
                    continue
                if not self._ogr_snf_gun_ok(task, gun):
                    continue
                if not self._ardisiklik_ok(task, start, gun, imkansiz):
                    continue
                if not self._gun_yayilim_ok(task, gun, sinif_key, grup_blok):
                    continue
                # Yerleştir
                for offset in range(task.blok):
                    for key in task.kaynak_anahtarlari():
                        self._used_slots[key].add(start + offset)
                for o in task.ogretmenler:
                    for c in task.siniflar:
                        self._ogr_snf_gun.add((o, c, gun))
                self._ders_gun[(task.ders, gun)].append((start, task.blok))
                self._sinif_ders_gunler[(sinif_key, task.ders)].append(gun)
                self.task_start[tid] = start
                yerlesti = True
                break

            if not yerlesti:
                log.debug(f"  [Greedy] task {tid} ({task.ders}, {task.siniflar}) yerleşemedi.")

        return self.task_start


# ════════════════════════════════════════════════════════════════════════════
#  MODÜL — MODEL BUILDER
#  Manuel day/hour ayrıştırması sayesinde AddDivisionEquality ve
#  AddModuloEquality KULLANILMAZ — performans avantajı büyüktür.
# ════════════════════════════════════════════════════════════════════════════

class ModelBuilder:
    """
    Her görev için şu değişkenleri kurar:
        day    ∈ [0, GUN_SAYISI-1]
        hour   ∈ [0, SAAT_SAYISI - blok]     ← gün bütünlüğü otomatik
        start  = day * SAAT_SAYISI + hour
        end    = start + blok
        (soft) presence ∈ {0, 1}

    Böylece Kısıt 2 (gün bütünlüğü) için ek bir kısıt gerekmez.
    """

    def __init__(self, model, tasks: List[Task]):
        self.model              = model
        self.tasks              = tasks
        self.task_vars          : Dict[int, Dict[str, Any]] = {}
        self.task_day_vars      : Dict[int, Any]   = {}
        self.task_hour_vars     : Dict[int, Any]   = {}
        self.resource_intervals : Dict[str, List[Any]] = defaultdict(list)
        self._yogunluk          : Dict[str, float] = {}
        # Kilit/izin slot takibi (çift ekleme önlemi)
        self._fixed_slots       : Dict[str, Set[int]] = defaultdict(set)

    # ── Yoğunluk hesabı (öğretmen bazlı) ────────────────────────────────────
    def _hesapla_yogunluk(self, ogretmen_izinleri: Dict) -> None:
        yuk: Dict[str, int] = defaultdict(int)
        for t in self.tasks:
            for o in t.ogretmenler:
                yuk[o] += t.blok
        for o, v in yuk.items():
            # set ile benzersizleştiriyoruz — aynı slot birden fazla listelenmişse
            kapali = len(set(ogretmen_izinleri.get(o, [])))
            musait = max(1, HORIZON - kapali)
            self._yogunluk[o] = v / musait

    def max_yogunluk(self, task: Task) -> float:
        return max(
            (self._yogunluk.get(o, 0.0) for o in task.ogretmenler), default=0.0
        )

    # ── Görev değişkenleri ──────────────────────────────────────────────────
    def insa_et(self, ogretmen_izinleri: Dict, soft: bool = False) -> None:
        self._hesapla_yogunluk(ogretmen_izinleri)
        sirali_tasks = sorted(
            self.tasks,
            key=lambda t: (-t.blok, -self.max_yogunluk(t), -len(t.ogretmenler), t.id),
        )

        for t in sirali_tasks:
            tid  = t.id
            blok = t.blok

            day   = self.model.NewIntVar(0, GUN_SAYISI - 1,    f"d_{tid}")
            hour  = self.model.NewIntVar(0, SAAT_SAYISI - blok, f"h_{tid}")
            start = self.model.NewIntVar(0, HORIZON - blok,     f"s_{tid}")
            end   = self.model.NewIntVar(blok, HORIZON,          f"e_{tid}")

            # Manuel ayrıştırma (AddDivisionEquality/AddModuloEquality kullanılmaz)
            self.model.Add(start == day * SAAT_SAYISI + hour)
            self.model.Add(end == start + blok)

            if soft:
                presence = self.model.NewBoolVar(f"p_{tid}")
                interval = self.model.NewOptionalIntervalVar(
                    start, blok, end, presence, f"i_{tid}"
                )
            else:
                presence = None
                interval = self.model.NewIntervalVar(start, blok, end, f"i_{tid}")

            self.task_day_vars[tid]  = day
            self.task_hour_vars[tid] = hour
            self.task_vars[tid] = {
                "start": start, "end": end, "day": day, "hour": hour,
                "presence": presence, "interval": interval, "blok": blok,
                "siniflar": t.siniflar, "ogretmenler": t.ogretmenler, "task": t,
            }
            for anahtar in set(t.kaynak_anahtarlari()):
                self.resource_intervals[anahtar].append(interval)

    # ── Kilit ve izinler → FixedInterval ────────────────────────────────────
# ── BAŞLANGIÇ: Düzeltilmiş kilit_ekle ──
    def kilit_ekle(self, kilitli_saatler: List[KilitliSaat]) -> None:
        """
        Ders içeren kilitleri ilgili Task değişkenine bağlar.
        Aynı slot ve ders için gelen mükerrer (öğretmen+sınıf) kilitleri süzer.
        """
        kilitli_gorevler: Set[int] = set()
        kilitli_slot_dersler: Set[Tuple[int, str]] = set()  # (slot_idx, ders) takibi

        for k in kilitli_saatler:
            t_idx = k.gun * SAAT_SAYISI + (k.saat - 1)
            if not (0 <= t_idx < HORIZON):
                continue

            # A) DERS KİLİDİ
            if k.ders:
                # Bu slota bu ders için zaten bir kart bağlandıysa mükerrer kilidi atla
                if (t_idx, k.ders) in kilitli_slot_dersler:
                    continue

                for t in self.tasks:
                    if t.id in kilitli_gorevler:
                        continue
                    
                    uygun_kaynak = (
                        (k.kaynak_turu == "ogretmen" and k.kaynak_adi in t.ogretmenler) or
                        (k.kaynak_turu == "sinif" and k.kaynak_adi in t.siniflar)
                    )
                    
                    if uygun_kaynak and t.ders == k.ders:
                        # Görevin başlangıcını bu slota mühürle
                        self.model.Add(self.task_vars[t.id]["day"] == k.gun)
                        self.model.Add(self.task_vars[t.id]["hour"] == (k.saat - 1))
                        self.model.Add(self.task_vars[t.id]["start"] == t_idx)
                        
                        pres = self.task_vars[t.id].get("presence")
                        if pres is not None:
                            self.model.Add(pres == 1)

                        kilitli_gorevler.add(t.id)
                        # Bu dersin kapladığı tüm blok saatlerini işaretle (örn: 2 saat)
                        for offset in range(t.blok):
                            kilitli_slot_dersler.add((t_idx + offset, k.ders))

                        log.info(f"  [Kilit Bağlandı] Task {t.id} ({t.ders}) -> Gün: {k.gun}, Saat: {k.saat}")
                        break

            # B) DERS İÇERMEYEN ENGEL KİLİDİ
            else:
                on_ek = "O" if k.kaynak_turu == "ogretmen" else "C"
                anahtar = f"{on_ek}_{k.kaynak_adi}"
                if t_idx in self._fixed_slots[anahtar]:
                    continue
                self._fixed_slots[anahtar].add(t_idx)
                name = _slug(f"lock_{anahtar}_{t_idx}")
                fixed = self.model.NewIntervalVar(t_idx, 1, t_idx + 1, name)
                self.resource_intervals[anahtar].insert(0, fixed)
    def izin_ekle(self, ogretmen_listesi: List[str], ogretmen_izinleri: Dict,
                  sinif_listesi: List[str], sinif_izinleri: Dict) -> None:
        """
        Öğretmen ve sınıfların kapalı saatlerini (izinlerini) rezerve slot olarak ekler.
        """
        def _ekle(kaynak_adi: str, izinler: List, on_ek: str):
            anahtar = f"{on_ek}_{kaynak_adi}"
            tur = "ogretmen" if on_ek == "O" else "sinif"
            
            # Bu kaynağın o saatte kilitli bir DERSİ var mı kontrol et
            kilitli_ders_slotlari = {
                k.gun * SAAT_SAYISI + (k.saat - 1)
                for k in getattr(self, "kilitli_saatler", [])
                if k.kaynak_turu == tur and k.kaynak_adi == kaynak_adi and k.ders
            }

            for item in izinler:
                saat_1, gun_0 = int(item[0]), int(item[1])
                t_idx = gun_0 * SAAT_SAYISI + (saat_1 - 1)
                if not (0 <= t_idx < HORIZON):
                    continue
                if t_idx in self._fixed_slots[anahtar]:
                    continue
                # EĞER BU SAATTE KİLİTLİ DERSİ VARSA İZİN ENGELİ KOYMA:
                if t_idx in kilitli_ders_slotlari:
                    continue

                self._fixed_slots[anahtar].add(t_idx)
                name = _slug(f"izin_{anahtar}_{t_idx}")
                fixed = self.model.NewIntervalVar(t_idx, 1, t_idx + 1, name)
                self.resource_intervals[anahtar].insert(0, fixed)

        for o in ogretmen_listesi:
            _ekle(o, ogretmen_izinleri.get(o, []), "O")
        for c in sinif_listesi:
            _ekle(c, sinif_izinleri.get(c, []), "C")
    # ── Warm-start hint'leri ────────────────────────────────────────────────
    def greedy_hint_ekle(self, greedy_starts: Dict[int, int]) -> int:
        aktarilan = 0
        for tid, start_val in greedy_starts.items():
            if tid not in self.task_vars:
                continue
            if not (0 <= start_val < HORIZON):
                continue
            self.model.AddHint(self.task_vars[tid]["start"], start_val)
            self.model.AddHint(self.task_vars[tid]["day"],   start_val // SAAT_SAYISI)
            self.model.AddHint(self.task_vars[tid]["hour"],  start_val %  SAAT_SAYISI)
            presence = self.task_vars[tid].get("presence")
            if presence is not None:
                self.model.AddHint(presence, 1)
            aktarilan += 1
        return aktarilan

    def repair_hint_ekle(self, onceki_solver, onceki_task_vars: Dict,
                         yerlesmeyen_idler: Set[int],
                         ogretmen_izinleri: Optional[Dict] = None) -> None:
        """
        Aşama 3: Aşama 2'de yerleşenleri SABİTLE (hint), havuzdakiler için
        en az yoğun müsait güne yönlendir.
        """
        # Yerleşmiş task'lerden gün yoğunluğu hesabı
        gun_yogunluk: Dict[int, int] = defaultdict(int)
        for t in self.tasks:
            if t.id in yerlesmeyen_idler:
                continue
            try:
                sv = onceki_solver.Value(onceki_task_vars[t.id]["start"])
                gun_yogunluk[sv // SAAT_SAYISI] += 1
            except Exception as e:
                log.debug(f"  [repair_hint] yoğunluk oku hata tid={t.id}: {e}")

        for t in self.tasks:
            tid      = t.id
            presence = self.task_vars[tid].get("presence")

            if tid not in yerlesmeyen_idler:
                # Eski yerleşim: sabitle
                try:
                    sv = onceki_solver.Value(onceki_task_vars[tid]["start"])
                    self.model.AddHint(self.task_vars[tid]["start"], sv)
                    self.model.AddHint(self.task_vars[tid]["day"],   sv // SAAT_SAYISI)
                    self.model.AddHint(self.task_vars[tid]["hour"],  sv %  SAAT_SAYISI)
                    if presence is not None:
                        self.model.AddHint(presence, 1)
                except Exception as e:
                    log.debug(f"  [repair_hint] warm-start hatası tid={tid}: {e}")
            else:
                # Havuzdaki: en az yoğun müsait güne + günün ortasına
                if presence is not None:
                    self.model.AddHint(presence, 1)
                izinli: Set[int] = set()
                if ogretmen_izinleri:
                    for o in t.ogretmenler:
                        for (_, gun_0) in ogretmen_izinleri.get(o, []):
                            izinli.add(gun_0)
                musait = [g for g in range(GUN_SAYISI) if g not in izinli] or list(range(GUN_SAYISI))
                en_az = min(musait, key=lambda g: gun_yogunluk.get(g, 0), default=0)
                # Günün ortasına hint (günün ilk saatine değil)
                hour_mid = max(0, (SAAT_SAYISI - t.blok) // 2)
                self.model.AddHint(self.task_vars[tid]["day"],   en_az)
                self.model.AddHint(self.task_vars[tid]["hour"],  hour_mid)
                self.model.AddHint(self.task_vars[tid]["start"], en_az * SAAT_SAYISI + hour_mid)

    def hafta_yayilim_hint(self, ogretmen_izinleri: Optional[Dict] = None) -> None:
        """Greedy yoksa hafta geneline dağıtıcı hint."""
        if not self._yogunluk:
            return
        ogr_tasklar: Dict[str, List[Task]] = defaultdict(list)
        for t in self.tasks:
            for o in t.ogretmenler:
                ogr_tasklar[o].append(t)

        hintlendi: Set[int] = set()
        for ogr, t_list in ogr_tasklar.items():
            yog = self._yogunluk.get(ogr, 0.0)
            if yog >= YOGUNLUK_ESIK_YUKSEK:
                baslangic = 0
            elif yog >= YOGUNLUK_ESIK_ORTA:
                baslangic = 1
            else:
                baslangic = 2
            izinli: Set[int] = set()
            if ogretmen_izinleri:
                for (_, g) in ogretmen_izinleri.get(ogr, []):
                    izinli.add(g)
            musait = [g for g in range(GUN_SAYISI) if g not in izinli] or list(range(GUN_SAYISI))
            sira = sorted(musait, key=lambda g: (g - baslangic) % GUN_SAYISI)
            idx = 0
            for t in t_list:
                if t.id in hintlendi:
                    continue
                hintlendi.add(t.id)
                gun = sira[idx % len(sira)]
                self.model.AddHint(self.task_vars[t.id]["day"],   gun)
                self.model.AddHint(self.task_vars[t.id]["hour"],  0)
                self.model.AddHint(self.task_vars[t.id]["start"], gun * SAAT_SAYISI)
                idx += 1


# ════════════════════════════════════════════════════════════════════════════
#  MODÜL — CONSTRAINT MANAGER
# ════════════════════════════════════════════════════════════════════════════

class ConstraintManager:
    """
    Tüm kısıtlar presence'a duyarlıdır (soft modda OnlyEnforceIf).
    Hard modda presence=None olduğu için tüm kısıtlar zorunludur.
    """

    def __init__(self, model, task_vars, task_day_vars,
                 resource_intervals, tasks, soft: bool = False,
                 ardisiklik_aktif: bool = True,
                 gelismis_ayarlar: dict = None):
        self.model              = model
        self.task_vars          = task_vars
        self.task_day_vars      = task_day_vars
        self.resource_intervals = resource_intervals
        self.tasks              = tasks
        self.soft               = soft
        self.ardisiklik_aktif   = ardisiklik_aktif
        self.gelismis_ayarlar   = gelismis_ayarlar or {}
        self._task_map: Dict[int, Task] = {t.id: t for t in tasks}

    def hepsini_ekle(self) -> None:
        self._no_overlap()
        self._simetri_kirma()
        self._gun_yayilimi()
        self._ogr_snf_ayni_gun()
        self._ders_iliskileri()
        if self.ardisiklik_aktif:
            self._ayni_ders_ardisiklik_yasagi()
        if self.soft:
            self._gunluk_iki_saatlik_blok_siniri()

    # ── 8. Özel Ders İlişki Kuralları ──────────────────────────────────────
    def _ders_iliskileri(self) -> None:
        kurallar = self.gelismis_ayarlar.get("ders_iliskileri", [])
        if not kurallar:
            return

        snf_tasks: Dict[str, List[Task]] = defaultdict(list)
        for t in self.tasks:
            for c in t.siniflar:
                snf_tasks[c].append(t)

        count = 0
        for snf, t_list in snf_tasks.items():
            for kural in kurallar:
                d1, d2, tip = kural.get("ders1"), kural.get("ders2"), kural.get("tip")
                if not d1 or not d2:
                    continue
                
                tasks1 = [t for t in t_list if t.ders == d1]
                tasks2 = [t for t in t_list if t.ders == d2]

                for t1 in tasks1:
                    for t2 in tasks2:
                        day1 = self.task_day_vars[t1.id]
                        day2 = self.task_day_vars[t2.id]
                        
                        p1 = self.task_vars[t1.id].get("presence")
                        p2 = self.task_vars[t2.id].get("presence")

                        enforcer = []
                        if self.soft and p1 is not None and p2 is not None:
                            enforcer = [p1, p2]

                        if tip == "ayni_gun":
                            kisit = self.model.Add(day1 == day2)
                        else:
                            kisit = self.model.Add(day1 != day2)

                        if enforcer:
                            kisit.OnlyEnforceIf(enforcer)
                        count += 1
                        
        log.info(f"  [Kısıt 8] Özel Ders İlişkileri: {count} kısıt")

    # ── 1. NoOverlap ────────────────────────────────────────────────────────
    def _no_overlap(self) -> None:
        n = 0
        for _, intervals in self.resource_intervals.items():
            if len(intervals) > 1:
                self.model.AddNoOverlap(intervals)
                n += 1
        log.info(f"  [Kısıt 1] NoOverlap: {n} kaynak")

    # ── 3. Simetri Kırma ────────────────────────────────────────────────────
    def _simetri_kirma(self) -> None:
        gruplar: Dict[Tuple, List[Task]] = defaultdict(list)
        for t in self.tasks:
            anahtar = (tuple(sorted(t.siniflar)), t.ders, tuple(sorted(t.ogretmenler)))
            gruplar[anahtar].append(t)

        count = 0
        for _, gt in gruplar.items():
            if len(gt) <= 1:
                continue
            sirali = sorted(gt, key=lambda x: (-x.blok, x.blok_idx))
            for i in range(len(sirali) - 1):
                if sirali[i].blok != sirali[i + 1].blok:
                    continue
                ti, tj = sirali[i].id, sirali[i + 1].id
                si, sj = self.task_vars[ti]["start"], self.task_vars[tj]["start"]
                pi, pj = self.task_vars[ti]["presence"], self.task_vars[tj]["presence"]
                if self.soft and pi is not None and pj is not None:
                    self.model.Add(si < sj).OnlyEnforceIf([pi, pj])
                else:
                    self.model.Add(si < sj)
                count += 1
        log.info(f"  [Kısıt 3] Simetri Kırma: {count} kısıt")

    # ── 4. Gün Yayılımı ─────────────────────────────────────────────────────
    def _gun_yayilimi(self) -> None:
        # <--- YENİ EKLENEN --->
        if not self.gelismis_ayarlar.get("gun_yayilimi", True):
            return
        # <-------------------->
        gruplar: Dict[Tuple, List[int]] = defaultdict(list)
        for t in self.tasks:
            anahtar = (tuple(sorted(t.siniflar)), t.ders)
            gruplar[anahtar].append(t.id)

        count = 0
        for _, id_list in gruplar.items():
            if len(id_list) <= 1:
                continue
            imkansiz = len(id_list) > GUN_SAYISI

            if (not self.soft) and (not imkansiz):
                common = set(self._task_map[id_list[0]].ogretmenler)
                for tid in id_list[1:]:
                    common &= set(self._task_map[tid].ogretmenler)
                if common:
                    day_vars = [self.task_day_vars[tid] for tid in id_list]
                    self.model.AddAllDifferent(day_vars)
                    count += 1
                    continue

            for i in range(len(id_list)):
                for j in range(i + 1, len(id_list)):
                    ti, tj = id_list[i], id_list[j]
                    pi, pj = self.task_vars[ti]["presence"], self.task_vars[tj]["presence"]
                    di, dj = self.task_day_vars[ti], self.task_day_vars[tj]
                    t_i, t_j = self._task_map[ti], self._task_map[tj]

                    enforcer = ([pi, pj] if (self.soft and pi is not None and pj is not None) else None)
                    ayni_ogretmen = bool(set(t_i.ogretmenler) & set(t_j.ogretmenler))
                    bi, bj = t_i.blok, t_j.blok
                    buyuk_blok_cifti = (bi >= 2 and bj >= 2)
                    farkli_gun_gerek = ayni_ogretmen or buyuk_blok_cifti

                    if farkli_gun_gerek and not imkansiz:
                        kisit = self.model.Add(di != dj)
                        if enforcer:
                            kisit.OnlyEnforceIf(enforcer)
                    else:
                        GAP = 2 if (farkli_gun_gerek and imkansiz) else 1
                        same_day = self.model.NewBoolVar(f"sd4_{ti}_{tj}")
                        self.model.Add(di == dj).OnlyEnforceIf(same_day)
                        self.model.Add(di != dj).OnlyEnforceIf(same_day.Not())
                        si, sj = self.task_vars[ti]["start"], self.task_vars[tj]["start"]
                        ei, ej = self.task_vars[ti]["end"],   self.task_vars[tj]["end"]
                        before = self.model.NewBoolVar(f"bef4_{ti}_{tj}")
                        after  = self.model.NewBoolVar(f"aft4_{ti}_{tj}")
                        b_enf = [same_day, before] + (enforcer or [])
                        a_enf = [same_day, after]  + (enforcer or [])
                        o_enf = [same_day]         + (enforcer or [])
                        self.model.Add(ei + GAP <= sj).OnlyEnforceIf(b_enf)
                        self.model.Add(ej + GAP <= si).OnlyEnforceIf(a_enf)
                        self.model.AddBoolOr([before, after]).OnlyEnforceIf(o_enf)
                    count += 1
        log.info(f"  [Kısıt 4] Gün Yayılımı: {count} kısıt")

    # ── 5. Öğretmen-Sınıf Aynı Gün ─────────────────────────────────────────
    def _ogr_snf_ayni_gun(self) -> None:
        # <--- YENİ EKLENEN --->
        if not self.gelismis_ayarlar.get("ogr_snf_gunluk_limit", True):
            return
        # <-------------------->
        ogr_snf: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for t in self.tasks:
            for o in t.ogretmenler:
                for c in t.siniflar:
                    ogr_snf[(o, c)].append(t.id)

        count = 0
        for _, id_list in ogr_snf.items():
            if len(id_list) <= 1:
                continue
            if (not self.soft) and len(id_list) <= GUN_SAYISI:
                day_vars = [self.task_day_vars[tid] for tid in id_list]
                self.model.AddAllDifferent(day_vars)
                count += 1
                continue
            for i in range(len(id_list)):
                for j in range(i + 1, len(id_list)):
                    ti, tj = id_list[i], id_list[j]
                    di, dj = self.task_day_vars[ti], self.task_day_vars[tj]
                    pi, pj = self.task_vars[ti]["presence"], self.task_vars[tj]["presence"]
                    if self.soft and pi is not None and pj is not None:
                        self.model.Add(di != dj).OnlyEnforceIf([pi, pj])
                    else:
                        self.model.Add(di != dj)
                    count += 1
        log.info(f"  [Kısıt 5] Öğretmen-Sınıf Günü: {count} kısıt")

    # ── 6. Aynı Ders Ardışıklık Yasağı ─────────────────────────────────────
    def _ayni_ders_ardisiklik_yasagi(self) -> None:
        # <--- YENİ EKLENEN --->
        gap = self.gelismis_ayarlar.get("ardisiklik_boslugu", 1)
        if gap <= 0:
            return
        # <-------------------->
        gruplar: Dict[Tuple, List[int]] = defaultdict(list)
        for t in self.tasks:
            anahtar = (tuple(sorted(t.siniflar)), t.ders)
            gruplar[anahtar].append(t.id)

        count = 0
        for _, id_list in gruplar.items():
            if len(id_list) < 2:
                continue
            if len(id_list) > GUN_SAYISI:
                continue 
            for i in range(len(id_list)):
                for j in range(i + 1, len(id_list)):
                    ti, tj = id_list[i], id_list[j]
                    di, dj = self.task_day_vars[ti], self.task_day_vars[tj]
                    pi, pj = self.task_vars[ti]["presence"], self.task_vars[tj]["presence"]
                    si, sj = self.task_vars[ti]["start"], self.task_vars[tj]["start"]
                    bi = self.task_vars[ti]["blok"]
                    bj = self.task_vars[tj]["blok"]

                    same_day = self.model.NewBoolVar(f"sd6_{ti}_{tj}")
                    self.model.Add(di == dj).OnlyEnforceIf(same_day)
                    self.model.Add(di != dj).OnlyEnforceIf(same_day.Not())

                    before = self.model.NewBoolVar(f"bef6_{ti}_{tj}")
                    after  = self.model.NewBoolVar(f"aft6_{ti}_{tj}")

                    enforcer_base = [same_day]
                    if self.soft and pi is not None and pj is not None:
                        enforcer_base.extend([pi, pj])

                    # <--- YENİ EKLENEN (GAP Dinamikleştirildi) --->
                    self.model.Add(sj >= si + bi + gap).OnlyEnforceIf(enforcer_base + [before])
                    self.model.Add(si >= sj + bj + gap).OnlyEnforceIf(enforcer_base + [after])
                    # <-------------------------------------------->
                    
                    self.model.AddBoolOr([before, after]).OnlyEnforceIf(enforcer_base)
                    count += 1
        log.info(f"  [Kısıt 6] Ardışıklık Yasağı (Boşluk: {gap}): {count} kısıt")

    # ── 7. Günlük 2-Saatlik Blok Limiti ────────────────────────────────────
    def _gunluk_iki_saatlik_blok_siniri(self) -> None:
        # <--- YENİ EKLENEN --->
        if not self.gelismis_ayarlar.get("gunluk_max_blok", True):
            return
        # <-------------------->
        gruplar: Dict[Tuple, List[int]] = defaultdict(list)
        for t in self.tasks:
            anahtar = (tuple(sorted(t.siniflar)), t.ders)
            gruplar[anahtar].append(t.id)

        count = 0
        for _, id_list in gruplar.items():
            iki_saatlik = [tid for tid in id_list if self._task_map[tid].blok == 2]
            if len(iki_saatlik) < 2:
                continue

            for g in range(GUN_SAYISI):
                gun_boollar = []
                for tid in iki_saatlik:
                    pres = self.task_vars[tid].get("presence")
                    is_on_g = self.model.NewBoolVar(f"iog_{tid}_{g}")
                    self.model.Add(self.task_day_vars[tid] == g).OnlyEnforceIf(is_on_g)
                    self.model.Add(self.task_day_vars[tid] != g).OnlyEnforceIf(is_on_g.Not())

                    if pres is not None:
                        b = self.model.NewBoolVar(f"gss_{tid}_{g}")
                        self.model.AddBoolAnd([is_on_g, pres]).OnlyEnforceIf(b)
                        self.model.AddBoolOr([is_on_g.Not(), pres.Not()]).OnlyEnforceIf(b.Not())
                        gun_boollar.append(b)
                    else:
                        gun_boollar.append(is_on_g)

                if len(gun_boollar) > 1:
                    self.model.Add(sum(gun_boollar) <= 1)
                    count += 1
        log.info(f"  [Kısıt 7] Günlük 2-Saatlik Blok Limiti (soft): {count} kısıt")

# ── Objective ──────────────────────────────────────────────────────────
    def objective_ekle(self, yogunluk: Dict[str, float], soft: bool = False) -> None:
        terms = []
        yogun_yon = self.gelismis_ayarlar.get("yogun_ders_egilimi", "yok")
        yogun_esik = self.gelismis_ayarlar.get("yogun_ders_saat_esigi", 5)
        
        for t in self.tasks:
            tid = t.id
            max_yog = max((yogunluk.get(o, 0.0) for o in t.ogretmenler), default=0.0)
            
            # Rastgeleliği artırmak için noise
            noise = random.randint(0, 100)
            base = (W_BASE
                   + int(max_yog * W_YOGUNLUK_MULT)
                   + t.saat * W_SAAT_MULT
                   + t.blok * W_BLOK_MULT
                   + noise) 

            if soft:
                presence = self.task_vars[tid]["presence"]
                if presence is not None:
                    terms.append(presence * (W_PRESENCE_MULT + base))
            else:
                # HARD MOD
                is_yogun_ders = (yogun_yon != "yok" and t.saat >= yogun_esik)
                
                if is_yogun_ders:
                    if yogun_yon == "ilk_saatler":
                        # 1. Normal sabah itme kuvveti (Dersi sabah saatlerine yönlendirir)
                        terms.append(self.task_vars[tid]["hour"] * 50) 
                        
                        # 2. ADALET KİLİDİ: B sınıfını kurtaran "Geç Saat" cezası
                        # Eğer ders 6., 7. veya 8. saate (index olarak 5 ve üzeri) kalırsa, 
                        # motora devasa bir ceza puanı yazıyoruz. Böylece A'yı hep sabaha, 
                        # B'yi hep akşama atmak yerine ikisini de ilk 5 saat içine dağıtıyor.
                        is_late = self.model.NewBoolVar(f"late_{tid}")
                        self.model.Add(self.task_vars[tid]["hour"] >= 5).OnlyEnforceIf(is_late)
                        self.model.Add(self.task_vars[tid]["hour"] < 5).OnlyEnforceIf(is_late.Not())
                        
                        terms.append(is_late * 1000) # Son 3 saate kalırsa affetmez, çok ağır ceza!
                        
                    elif yogun_yon == "son_saatler":
                        # Dersi sona itme
                        terms.append(-self.task_vars[tid]["hour"] * 100)
                else:
                    # Yoğun olmayan dersler için programın farklı çıkmasını sağlayan rastgele itme
                    varyasyon_yonu = random.choice([-1, 0, 1])
                    rastgele_agirlik = base * varyasyon_yonu
                    terms.append(self.task_vars[tid]["start"] * rastgele_agirlik)

        # Çözücüye hedefi ver
        if soft:
            self.model.Maximize(sum(terms))
        else:
            self.model.Minimize(sum(terms))


# ════════════════════════════════════════════════════════════════════════════
#  MODÜL — RESULT REPORTER
# ════════════════════════════════════════════════════════════════════════════

class ResultReporter:
    """
    Solver sonucunu haftalık program matrisine dönüştürür.
    INFEASIBLE durumda ConflictMiner'ı devreye alır.

    ÖNEMLİ (v9.0.1): Havuz (kalan) kartları 1 SAATLİK birim olarak üretilir.
    Yerleşemeyen 2 saatlik bir blok, havuza 2 ayrı 1 saatlik kart olarak girer.
    Böylece programdaki 1 saatlik boş hücreler ile havuzdaki kartlar birebir
    eşleşir ve kullanıcı kalan dersleri elle yerleştirebilir.
    """

    def __init__(self, solver, task_vars, tasks, sinif_listesi,
                 ogretmen_izinleri, sinif_izinleri, kilitli_saatler,
                 sure_saniye: float, gelismis_ayarlar: dict = None):
        self.solver            = solver
        self.task_vars         = task_vars
        self.tasks             = tasks
        self.sinif_listesi     = sinif_listesi
        self.ogretmen_izinleri = ogretmen_izinleri
        self.sinif_izinleri    = sinif_izinleri
        self.kilitli_saatler   = kilitli_saatler
        self.sure_saniye       = sure_saniye
        self.gelismis_ayarlar  = gelismis_ayarlar or {}  # FIX: HATA-1

    def sonuc_uret(self, status, durum_str: str, soft: bool = False) -> Dict:
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            prog, kalan = self._prog_olustur(soft=soft)
            prog, ihlaller = self._dogrula_ve_duzelt(prog)
            if ihlaller:
                log.warning(f"  {len(ihlaller)} çakışma otomatik temizlendi.")
                for ih in ihlaller:
                    kalan.append(self._ihlal_to_dict(ih))

            # Post-placement kapasite uyarıları
            uyarilar = self._kapasite_uyari_kontrol(prog, kalan)
            if uyarilar:
                log.warning(f"  [KAPASİTE UYARISI] {len(uyarilar)} sınıf/öğretmen için "
                            f"havuz > boşluk tespit edildi:")
                for u in uyarilar:
                    log.warning(f"    ✗ {u}")

            return {
                "durum"       : "TAMAM",
                "program"     : prog,
                "kalan"       : kalan,
                "hata_mesaji" : "\n".join(uyarilar) if uyarilar else "",
                "motor_durum" : durum_str,
                "uyarilar"    : uyarilar,
            }
        if status == cp_model.INFEASIBLE:
            log.warning("  [ResultReporter] INFEASIBLE — ConflictMiner çağrılıyor...")
            miner = ConflictMiner(self.tasks, self.ogretmen_izinleri,
                                  self.sinif_izinleri, self.kilitli_saatler,
                                  self.sure_saniye, gelismis_ayarlar=self.gelismis_ayarlar)
            rapor = miner.analiz()
            return {
                "durum"       : "INFEASIBLE",
                "program"     : None,
                # Her task, t.blok kadar 1-saatlik kart üretir:
                "kalan"       : [self._task_to_dict(t)
                                 for t in self.tasks
                                 for _ in range(t.blok)],
                "hata_mesaji" : rapor,
                "motor_durum" : "INFEASIBLE",
            }
        return {
            "durum"       : "HATA",
            "program"     : None,
            # Her task, t.blok kadar 1-saatlik kart üretir:
            "kalan"       : [self._task_to_dict(t)
                             for t in self.tasks
                             for _ in range(t.blok)],
            "hata_mesaji" : f"Süre doldu veya çözüm bulunamadı ({durum_str}).",
            "motor_durum" : durum_str,
        }

    def _prog_olustur(self, soft: bool = False) -> Tuple[Dict, List]:
        """
        Yerleşenleri program matrisine yazar; yerleşemeyen her task için
        t.blok kadar 1-saatlik havuz kartı üretir (havuz ↔ boş hücre uyumu).
        """
        prog: Dict = {
            g: {s: {snf: None for snf in self.sinif_listesi}
                for s in range(1, SAAT_SAYISI + 1)}
            for g in range(GUN_SAYISI)
        }
        kalan: List[Dict] = []

        for t in self.tasks:
            tid = t.id
            presence = self.task_vars[tid]["presence"]
            if soft and presence is not None and not self.solver.Value(presence):
                # ── FIX v9.0.1 ──
                # Yerleşemeyen task için t.blok kadar 1-saatlik kart üret.
                # Eski v9.0 davranışı: tek kart saat=t.blok → havuzdaki blok
                # 1 saatlik boş hücrelere sığmıyordu. Yeni davranış: 1+1+...
                for _ in range(t.blok):
                    kalan.append(self._task_to_dict(t))
                continue
            st  = self.solver.Value(self.task_vars[tid]["start"])
            gun = st // SAAT_SAYISI
            s0  = st %  SAAT_SAYISI
            if not (0 <= gun < GUN_SAYISI):
                log.error(f"  [_prog_olustur] Task {tid} geçersiz gün: {gun}. Atlandı.")
                # Yine t.blok kadar 1-saatlik kart (tutarlılık için)
                for _ in range(t.blok):
                    kalan.append(self._task_to_dict(t))
                continue
            for b in range(t.blok):
                saat_1 = s0 + b + 1
                if saat_1 > SAAT_SAYISI:
                    log.error(
                        f"  [_prog_olustur] Task {tid} saat taşması: "
                        f"gun={gun}, s0={s0}, blok={t.blok}. Parça atlandı."
                    )
                    continue
                for c in t.siniflar:
                    if c in self.sinif_listesi:
                        prog[gun][saat_1][c] = {
                            "ders"     : t.ders,
                            "ogretmen" : " + ".join(t.ogretmenler),
                            "task_id"  : tid,
                            "blok"     : t.blok,
                        }
        import re
        def _dogal_key(item):
            snf = str(item.get("sinif", ""))
            # 9A, 10A gibi sınıfları doğal sıralamak için sayıları doldur
            snf_parca = [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', snf)]
            ogr = str(item.get("ogretmen", "")).lower()
            drs = str(item.get("ders", "")).lower()
            return (snf_parca, ogr, drs)

        kalan.sort(key=_dogal_key)
        return prog, kalan

    def _kapasite_uyari_kontrol(self, prog: Dict, kalan: List[Dict]) -> List[str]:
        """
        Yerleştirme sonrası gerçekçi yerleştirilebilirlik denetimi.

        Her havuz öğesi (sınıf + öğretmen + ders) için ORTAK BOŞ slot sayısı
        hesaplanır:
            Ortak boş = (sınıf boş) ∩ (öğretmen boş)
                      = hem sınıfın hem öğretmenin eş zamanlı müsait olduğu slotlar

        Ortak boş < havuzdaki saat → bu ders matematiksel olarak sığmaz → UYARI.

        Eski versiyon sadece "sınıf boş hücre sayısı" bakıyordu — öğretmen
        müsaitliğini görmezden geldiği için imkânsız durumları kaçırıyordu.

        NOT (v9.0.1): Havuz kartları artık 1 saatlik birimlerdir. Aynı
        (sinif, ders, ogretmen) tuple'ı için saat ihtiyacı, o tuple'ın
        havuzdaki kart sayısına eşittir. Dolayısıyla önce grupluyoruz,
        sonra toplam saati hesaplıyoruz ve her grup için TEK uyarı yayıyoruz.
        """
        if not kalan:
            return []

        uyarilar: List[str] = []

        # ── Öğretmen meşgul slotları: placed lessons + izin + kilitli ────────
        ogr_mesgul: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)
        # placed lessons
        for g in range(GUN_SAYISI):
            for s in range(1, SAAT_SAYISI + 1):
                for snf in self.sinif_listesi:
                    cell = prog[g][s].get(snf)
                    if cell:
                        for o in cell["ogretmen"].split(" + "):
                            o = o.strip()
                            if o:
                                ogr_mesgul[o].add((g, s))
        # izin
        for o, izinler in self.ogretmen_izinleri.items():
            for (s1, g0) in izinler:
                if 0 <= g0 < GUN_SAYISI and 1 <= s1 <= SAAT_SAYISI:
                    ogr_mesgul[o].add((g0, s1))
        # kilitli
        for k in self.kilitli_saatler:
            if k.kaynak_turu == "ogretmen" and 0 <= k.gun < GUN_SAYISI and 1 <= k.saat <= SAAT_SAYISI:
                ogr_mesgul[k.kaynak_adi].add((k.gun, k.saat))

        # ── Sınıf meşgul slotları: placed lessons + izin + kilitli ──────────
        snf_mesgul: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)
        # placed lessons
        for g in range(GUN_SAYISI):
            for s in range(1, SAAT_SAYISI + 1):
                for snf in self.sinif_listesi:
                    if prog[g][s].get(snf) is not None:
                        snf_mesgul[snf].add((g, s))
        # izin
        for snf, izinler in self.sinif_izinleri.items():
            for (s1, g0) in izinler:
                if 0 <= g0 < GUN_SAYISI and 1 <= s1 <= SAAT_SAYISI:
                    snf_mesgul[snf].add((g0, s1))
        # kilitli
        for k in self.kilitli_saatler:
            if k.kaynak_turu == "sinif" and 0 <= k.gun < GUN_SAYISI and 1 <= k.saat <= SAAT_SAYISI:
                snf_mesgul[k.kaynak_adi].add((k.gun, k.saat))

        # Tüm slotlar kümesi
        tum_slotlar: Set[Tuple[int, int]] = {
            (g, s) for g in range(GUN_SAYISI) for s in range(1, SAAT_SAYISI + 1)
        }

        # ── FIX v9.0.1: Havuzu (sinif, ders, ogretmen) bazında grupla ────────
        # Her item artık 1 saatlik olduğu için aynı grup için birden fazla
        # item olabilir; toplam saat ihtiyacı = grup içindeki item sayısıdır.
        gruplar: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for item in kalan:
            snf_str = item.get("sinif", "")
            drs_str = item.get("ders", "")
            ogr_str = item.get("ogretmen", "")
            anahtar = (snf_str, drs_str, ogr_str)
            if anahtar not in gruplar:
                gruplar[anahtar] = {
                    "sinif": snf_str,
                    "ders":  drs_str,
                    "ogretmen": ogr_str,
                    "saat_toplam": 0,
                }
            gruplar[anahtar]["saat_toplam"] += int(item.get("saat", 1))

        # ── Her havuz grubu için ortak boş slot denetimi (tek uyarı / grup) ──
        for _, grup in gruplar.items():
            snf_listesi = [x.strip() for x in grup["sinif"].split(" + ")
                           if x.strip() in self.sinif_listesi]
            ogr_listesi = [x.strip() for x in grup["ogretmen"].split(" + ")
                           if x.strip()]
            saat_gerek  = grup["saat_toplam"]
            ders        = grup["ders"] or "?"

            if not snf_listesi:
                continue

            # Sınıfların birleşik meşgul kümesi
            snf_union_mesgul: Set[Tuple[int, int]] = set()
            for snf in snf_listesi:
                snf_union_mesgul |= snf_mesgul.get(snf, set())

            # Öğretmenlerin birleşik meşgul kümesi
            ogr_union_mesgul: Set[Tuple[int, int]] = set()
            for o in ogr_listesi:
                ogr_union_mesgul |= ogr_mesgul.get(o, set())

            # Ortak boş = tüm slotlar − (sınıf meşgul ∪ öğretmen meşgul)
            ortak_bos = tum_slotlar - snf_union_mesgul - ogr_union_mesgul
            ortak_bos_sayi = len(ortak_bos)

            if ortak_bos_sayi < saat_gerek:
                sinif_str = grup["sinif"] or "?"
                ogr_str   = grup["ogretmen"] or "?"
                uyarilar.append(
                    f"[YERLEŞEMEz] '{sinif_str}' — {ders} ({ogr_str}): "
                    f"Ortak boş slot={ortak_bos_sayi}, Gereken={saat_gerek} "
                    f"→ {saat_gerek - ortak_bos_sayi} saat matematiksel olarak SIĞMAZ."
                )

        return uyarilar

    def _dogrula_ve_duzelt(self, prog: Dict) -> Tuple[Dict, List[Dict]]:
        """
        Post-solve sağlamlık kontrolü: NoOverlap vs. her şeye rağmen aynı
        öğretmen aynı saatte farklı sınıflarda görünüyorsa (NADİR) temizler.
        """
        ihlaller: List[Dict] = []
        for g in range(GUN_SAYISI):
            for s in range(1, SAAT_SAYISI + 1):
                # Saat/gün için öğretmen -> (sınıf, task_id) listesi
                ogr_kayit: Dict[str, List[Tuple[str, Optional[int]]]] = defaultdict(list)
                for snf in self.sinif_listesi:
                    cell = prog[g][s].get(snf)
                    if not cell:
                        continue
                    tid = cell.get("task_id")
                    for o in cell["ogretmen"].split(" + "):
                        ogr_kayit[o.strip()].append((snf, tid))

                for ogr, kayit_listesi in ogr_kayit.items():
                    if len(kayit_listesi) <= 1:
                        continue
                    # Aynı task_id'den birden çok kayıt = birleşik sınıf (OK)
                    task_ids = {tid for (_, tid) in kayit_listesi if tid is not None}
                    if len(task_ids) <= 1:
                        continue
                    # Gerçek çakışma: ilkini koru, diğerlerini çıkar
                    for (snf, tid) in kayit_listesi[1:]:
                        cell = prog[g][s].get(snf)
                        ihlaller.append({
                            "gun": g, "saat": s, "sinif": snf,
                            "ogretmen": ogr,
                            "ders": cell.get("ders") if cell else "?",
                            "task_id": tid,
                        })
                        prog[g][s][snf] = None
        return prog, ihlaller

    def _ihlal_to_dict(self, ih: Dict) -> Dict:
        """İhlal kaydını 1-saatlik havuz kartına çevirir."""
        tid = ih.get("task_id")
        if tid is not None:
            for t in self.tasks:
                if t.id == tid:
                    # _task_to_dict zaten 1 saatlik kart üretir
                    return self._task_to_dict(t)
        return {
            "sinif"    : ih.get("sinif", "?"),
            "ders"     : ih.get("ders", "?"),
            "ogretmen" : ih.get("ogretmen", "?"),
            "saat"     : 1,
        }

    @staticmethod
    def _task_to_dict(t: Task) -> Dict:
        """
        Havuz kartı: HER BİRİ 1 SAAT. Bir task'in t.blok saatlik olması
        gerekiyorsa, çağıran taraf bu fonksiyonu t.blok kez çağırır.
        (Bkz. _prog_olustur ve sonuc_uret INFEASIBLE/HATA dalları.)
        """
        return {
            "sinif"    : " + ".join(t.siniflar),
            "ders"     : t.ders,
            "ogretmen" : " + ".join(t.ogretmenler),
            "saat"     : 1,
        }


# ════════════════════════════════════════════════════════════════════════════
#  MODÜL — CONFLICT MINER
# ════════════════════════════════════════════════════════════════════════════

class ConflictMiner:
    """
    INFEASIBLE sonucunda hangi öğretmen/sınıfın tıkadığını gösterir.
    Mini-model tüm kısıtları (1-6) + assumption'ları çalıştırır.
    """

    MIN_SURE = 15.0
    MAX_SURE = 90.0
    SURE_ORAN = 0.15

    def __init__(self, tasks, ogretmen_izinleri, sinif_izinleri, kilitli_saatler,
                 sure_saniye: float = 60.0, gelismis_ayarlar: Optional[Dict] = None, *args, **kwargs):
        self.tasks             = tasks
        self.ogretmen_izinleri = ogretmen_izinleri
        self.sinif_izinleri    = sinif_izinleri
        self.kilitli_saatler   = kilitli_saatler
        self.sure_saniye       = sure_saniye
        self.gelismis_ayarlar  = gelismis_ayarlar or {}
        self.hatalar: List[str] = []
        # Hızlı lookup için önden set'ler kurulur
        self._lock_sets: Dict[Tuple[str, str], Set[int]] = defaultdict(set)
        
        # YALNIZCA ders içermeyen (gerçekten kapalı olan) kilitleri kapasiteden düş
        for k in self.kilitli_saatler:
            if not k.ders:
                t_idx = k.gun * SAAT_SAYISI + (k.saat - 1)
                if 0 <= t_idx < HORIZON:
                    self._lock_sets[(k.kaynak_turu, k.kaynak_adi)].add(t_idx)
        
        # YALNIZCA ders içermeyen (gerçekten kapalı olan) kilitleri kapasiteden düş
        for k in self.kilitli_saatler:
            if not k.ders:
                t_idx = k.gun * SAAT_SAYISI + (k.saat - 1)
                if 0 <= t_idx < HORIZON:
                    self._lock_sets[(k.kaynak_turu, k.kaynak_adi)].add(t_idx)

    def analiz(self) -> str:
        mine_sure = max(self.MIN_SURE, min(self.MAX_SURE, self.sure_saniye * self.SURE_ORAN))
        log.info(f"  [ConflictMiner] Mini-model ({mine_sure:.0f}s)...")

        model   = cp_model.CpModel()
        builder = ModelBuilder(model, self.tasks)
        builder.insa_et(self.ogretmen_izinleri, soft=True)
        builder.kilit_ekle(self.kilitli_saatler)
        # ConflictMiner için ogretmen/sınıf listelerini task'lerden derleriz
        ogr_set = {o for t in self.tasks for o in t.ogretmenler}
        snf_set = {c for t in self.tasks for c in t.siniflar}
        builder.izin_ekle(sorted(ogr_set), self.ogretmen_izinleri,
                          sorted(snf_set), self.sinif_izinleri)

        # Tüm kısıtlar aktif (Kısıt 3 dahil)
        cm = ConstraintManager(model, builder.task_vars, builder.task_day_vars,
                               builder.resource_intervals, self.tasks,
                               soft=True, ardisiklik_aktif=True,
                               gelismis_ayarlar=self.gelismis_ayarlar)
        cm.hepsini_ekle()

        # Assumption: her kaynak için "tüm tasks yerleşsin" koşulu
        ogr_tasks: Dict[str, List[int]] = defaultdict(list)
        snf_tasks: Dict[str, List[int]] = defaultdict(list)
        for t in self.tasks:
            for o in t.ogretmenler:
                ogr_tasks[o].append(t.id)
            for c in t.siniflar:
                snf_tasks[c].append(t.id)

        assumptions: List[Any] = []
        assumption_label: Dict[int, str] = {}

        for ogr, tid_list in sorted(ogr_tasks.items()):
            a = model.NewBoolVar(_slug(f"assume_O_{ogr}"))
            for tid in tid_list:
                model.Add(builder.task_vars[tid]["presence"] == 1).OnlyEnforceIf(a)
            assumptions.append(a)
            assumption_label[a.Index()] = f"Öğretmen '{ogr}'"

        for snf, tid_list in sorted(snf_tasks.items()):
            a = model.NewBoolVar(_slug(f"assume_C_{snf}"))
            for tid in tid_list:
                model.Add(builder.task_vars[tid]["presence"] == 1).OnlyEnforceIf(a)
            assumptions.append(a)
            assumption_label[a.Index()] = f"Sınıf '{snf}'"

        model.AddAssumptions(assumptions)
        solver = cp_model.CpSolver()
        solver.parameters.random_seed = random.randint(1, 100000)
        solver.parameters.max_time_in_seconds = mine_sure
        solver.parameters.num_search_workers  = min(4, os.cpu_count() or 4)
        solver.parameters.log_search_progress = False
        status = solver.Solve(model)

        if status == cp_model.INFEASIBLE:
            try:
                idxs = solver.SufficientAssumptionsForInfeasibility()
            except Exception as e:
                log.debug(f"  [ConflictMiner] SufficientAssumptions hata: {e}")
                idxs = []
            isimler = [assumption_label.get(i, f"Bilinmeyen(idx={i})") for i in idxs]
            if isimler:
                line = "━" * 50
                r  = f"{line}\n  ÇAKIŞMA ANALİZİ RAPORU\n{line}\n\n"
                r += "Aşağıdaki kaynaklar birbiriyle çakışıyor:\n\n"
                for cn in isimler:
                    r += f"  ⛔  {cn}\n"
                r += "\nÖneriler:\n"
                r += "  1. Haftalık ders saatini azaltın.\n"
                r += "  2. İlgili kaynakların izin/kapalı saatlerini azaltın.\n"
                r += "  3. Birleştirilmiş blokları (2+2) parçalayın.\n"
                r += f"{line}"
                return r

        return ("INFEASIBLE: ConflictMiner ek bilgi üretemedi. "
                "Ders yükü ve izinleri manuel inceleyiniz.")


# ════════════════════════════════════════════════════════════════════════════
#  MODÜL — EXPERT REPORTER
# ════════════════════════════════════════════════════════════════════════════

class ExpertReporter:
    """Havuzda kalan derslerin NEDEN yerleşemediğini analiz eder."""

    @staticmethod
    def analiz_et(tasks, kalanlar, ogretmen_izinleri, sinif_izinleri) -> str:
        if not kalanlar:
            return "Tüm dersler başarıyla yerleşti!"
        rapor = [
            "\n" + "═" * 60,
            "🔍 MOTOR v9.0.1 — PROBLEM ANALİZİ",
            "═" * 60,
            f"\n⚠️ TOPLAM {len(kalanlar)} SAAT DIŞARIDA KALDI.\n",
            "🚨 TIKANIKLIK ANALİZİ:",
        ]

        # Havuz kartları 1-saatlik birimler (v9.0.1) — aynı
        # (ogr, snf, ders) için birden fazla kart olabilir; grup bazında
        # tekil analiz yapıyoruz.
        gruplar: Dict[Tuple[str, str, str], int] = defaultdict(int)
        for k in kalanlar:
            anahtar = (k.get("ogretmen", "?"), k.get("sinif", "?"), k.get("ders", "?"))
            gruplar[anahtar] += 1

        for (ogr_str, snf_str, ders), kalan_saat in gruplar.items():  # FIX HATA-4
            # Birleşik sınıf/öğretmen: ilk isimle analiz yap
            ogr = ogr_str.split(" + ")[0].strip()
            snf = snf_str.split(" + ")[0].strip()
            ilgili = [t for t in tasks
                      if ogr in t.ogretmenler and snf in t.siniflar and t.ders == ders]
            if not ilgili:
                rapor.append(f"  • [BİLİNMEYEN] {ogr_str} - {snf_str} ({ders}): {kalan_saat} saat")
                continue

            gereken_gun = len(ilgili)
            max_blok = max(t.blok for t in ilgili)

            ogr_izinli_map: Dict[int, Set[int]] = defaultdict(set)
            for (s1, g0) in ogretmen_izinleri.get(ogr, []):
                ogr_izinli_map[g0].add(s1)
            snf_izinli_map: Dict[int, Set[int]] = defaultdict(set)
            for (s1, g0) in sinif_izinleri.get(snf, []):
                snf_izinli_map[g0].add(s1)

            musait_gun = 0
            for g in range(GUN_SAYISI):
                bos = [s for s in range(1, SAAT_SAYISI + 1)
                       if s not in ogr_izinli_map[g] and s not in snf_izinli_map[g]]
                # Ardışık max_blok boşluk var mı?
                yeterli = False
                for idx in range(len(bos) - max_blok + 1):
                    if all(bos[idx + k] == bos[idx] + k for k in range(max_blok)):
                        yeterli = True
                        break
                if yeterli:
                    musait_gun += 1

            if musait_gun < gereken_gun:
                rapor.append(f"  • [İMKANSIZ] {ogr_str} — {snf_str} ({ders}): {kalan_saat} saat dışarıda")
                rapor.append(
                    f"    ➔ {gereken_gun} farklı gün gerek; "
                    f"{max_blok}-saatlik ortak müsait gün sayısı yalnızca {musait_gun}."
                )
            else:
                rapor.append(f"  • [SIKIŞIKLIK] {ogr_str} — {snf_str} ({ders}): {kalan_saat} saat dışarıda")
                rapor.append(
                    "    ➔ Teorik olarak sığıyor; pratikte diğer derslerin "
                    "yoğunluğu nedeniyle yer bulunamadı."
                )

        rapor.append("\n💡 ÇÖZÜM İÇİN:")
        rapor.append("  1. 'İMKANSIZ' için izinli günleri azaltın / ders saatini düşürün.")
        rapor.append("  2. 'SIKIŞIKLIK' için motor süresini artırın (sure_saniye).")
        rapor.append("═" * 60)
        return "\n".join(rapor)


# ════════════════════════════════════════════════════════════════════════════
#  HELPER FONKSİYONLAR
# ════════════════════════════════════════════════════════════════════════════

def _sutun_bul(cols: List[str], arayanlar: List[str]) -> Optional[str]:
    """Kolonlar arasında normalleştirilmiş eşleşme ara. Yoksa None döner."""
    for a in arayanlar:
        for c in cols:
            if a in _norm_tr(c):
                return c
    return None


def _df_normalize(df) -> Tuple[Any, str, str, str, str]:
    """Gerekli kolonları bulur. Bulamazsa açık ValueError fırlatır."""
    cols = list(df.columns)
    c0 = _sutun_bul(cols, ["sinif", "class"])
    c1 = _sutun_bul(cols, ["ders", "lesson", "subject"])
    c2 = _sutun_bul(cols, ["ogretmen", "teacher"])
    c3 = _sutun_bul(cols, ["saat", "hour"])
    eksik = [isim for isim, v in
             [("sinif", c0), ("ders", c1), ("ogretmen", c2), ("saat", c3)] if v is None]
    if eksik:
        raise ValueError(
            f"Ders yükü tablosunda eksik kolon(lar): {', '.join(eksik)}. "
            f"Mevcut kolonlar: {cols}"
        )
    return df, c0, c1, c2, c3


def _parse_saat(val) -> int:
    """'4', '4.0', 4, ' 4 ' → 4. Geçersizse 0."""
    try:
        return int(float(str(val).strip().replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def _gorev_olustur(df, c0: str, c1: str, c2: str, c3: str,
                   blok_stratejisi="ikili") -> List[Task]:
    """DataFrame satırlarını Task listesine dönüştürür."""
    tasks: List[Task] = []
    task_id = 0
    for _, row in df.iterrows():
        try:
            snflar = list(dict.fromkeys(s.strip() for s in str(row[c0]).split("+") if s.strip()))
            ogrs = list(dict.fromkeys(o.strip() for o in str(row[c2]).split("+") if o.strip()))
            drs  = str(row[c1]).strip()
            saat = _parse_saat(row[c3])
        except Exception as e:
            log.warning(f"  [_gorev_olustur] Satır atlandı: {e}")
            continue
        if not snflar or not ogrs or not drs or saat <= 0:
            continue

        # <--- YENİ BLOK STRATEJİSİ MANTIĞI --->
        if blok_stratejisi == "tekli":
            bloklar = [1] * saat
        else:
            bloklar = varsayilan_blok_bolumleme(saat)
        # <------------------------------------>

        for blok_idx, b in enumerate(bloklar):
            tasks.append(Task(
                id=task_id, siniflar=snflar, ogretmenler=ogrs, ders=drs,
                blok=b, blok_idx=blok_idx, toplam_blok=len(bloklar), saat=saat,
            ))
            task_id += 1
    return tasks


def _kilitli_saatler_isle(kilitli_ham) -> List[KilitliSaat]:
    if not kilitli_ham:
        return []
    out: List[KilitliSaat] = []
    for item in kilitli_ham:
        try:
            out.append(KilitliSaat(
                kaynak_turu = str(item.get("kaynak_turu", "ogretmen")),
                kaynak_adi  = str(item["kaynak_adi"]),
                gun         = int(item["gun"]),
                saat        = int(item["saat"]),
                ders        = item.get("ders"),
            ))
        except Exception as e:
            log.warning(f"  Geçersiz kilit atlandı: {item} -> {e}")
    return out


def _model_kur(
    tasks, kilitler,
    ogretmen_listesi, ogretmen_izinleri,
    sinif_listesi, sinif_izinleri,
    yogunluk_ref: Optional[Dict] = None,
    soft: bool = False,
    greedy_hints: Optional[Dict[int, int]] = None,
    onceki_solver=None,
    onceki_task_vars: Optional[Dict] = None,
    yerlesmeyen_idler: Optional[Set[int]] = None,
    ardisiklik_aktif: bool = True,
    gelismis_ayarlar: Optional[Dict] = None,
) -> Tuple[Any, ModelBuilder]:
    """Bir aşama için komple model kurar (builder + constraints + objective)."""
    model   = cp_model.CpModel()
    builder = ModelBuilder(model, tasks)
    builder.insa_et(ogretmen_izinleri, soft=soft)
    if yogunluk_ref is not None:
        builder._yogunluk = yogunluk_ref
    builder.kilit_ekle(kilitler)
    builder.izin_ekle(ogretmen_listesi, ogretmen_izinleri,
                      sinif_listesi, sinif_izinleri)

    if onceki_solver is not None and onceki_task_vars is not None and yerlesmeyen_idler is not None:
        builder.repair_hint_ekle(onceki_solver, onceki_task_vars,
                                 yerlesmeyen_idler, ogretmen_izinleri)
    elif greedy_hints:
        n = builder.greedy_hint_ekle(greedy_hints)
        log.info(f"  [Hint] {n}/{len(tasks)} warm-start hint eklendi.")
    elif not soft:
        builder.hafta_yayilim_hint(ogretmen_izinleri)

    cm = ConstraintManager(model, builder.task_vars, builder.task_day_vars,
                           builder.resource_intervals, tasks,
                           soft=soft, ardisiklik_aktif=ardisiklik_aktif,
                           gelismis_ayarlar=gelismis_ayarlar)
    cm.hepsini_ekle()
    cm.objective_ekle(builder._yogunluk, soft=soft)
    return model, builder


def _solver_parametreleri(sure: float, verbose: bool = False):
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds    = max(1.0, sure)
    s.parameters.num_search_workers     = min(SOLVER_WORKER_MAX, os.cpu_count() or 4)
    s.parameters.log_search_progress    = verbose
    s.parameters.cp_model_presolve      = True
    s.parameters.linearization_level    = 2
    s.parameters.search_branching       = cp_model.PORTFOLIO_WITH_QUICK_RESTART_SEARCH
    s.parameters.use_optimization_hints = True
    s.parameters.random_seed            = random.randint(1, 2_000_000_000)
    
    return s


def _durum_str(status) -> str:
    return {
        cp_model.OPTIMAL   : "OPTIMAL",
        cp_model.FEASIBLE  : "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.UNKNOWN   : "UNKNOWN",
    }.get(status, str(status))


def _yerlesmeyen_bul(solver, builder, tasks) -> Set[int]:
    out: Set[int] = set()
    for t in tasks:
        p = builder.task_vars[t.id]["presence"]
        if p is not None and not solver.Value(p):
            out.add(t.id)
    return out


def _lns_swap_repair(
    mevcut_solver, mevcut_builder, tasks, yerlesmeyen_idler,
    sure_limit: float,
    ogretmen_izinleri, sinif_izinleri, kilitler,
    ogretmen_listesi, sinif_listesi,
    yogunluk_ref: Dict,
    gelismis_ayarlar: Optional[Dict] = None
) -> Tuple[Optional[Any], Optional[ModelBuilder], Set[int]]:
    if not yerlesmeyen_idler:
        return None, None, set(yerlesmeyen_idler)

    task_map = {t.id: t for t in tasks}

    komsu_ids: Set[int] = set()
    for tid in yerlesmeyen_idler:
        key_set = set(task_map[tid].kaynak_anahtarlari())
        for other in tasks:
            if other.id == tid or other.id in yerlesmeyen_idler:
                continue
            if set(other.kaynak_anahtarlari()) & key_set:
                komsu_ids.add(other.id)

    if not komsu_ids:
        return None, None, set(yerlesmeyen_idler)

    max_komsu = max(4, len(yerlesmeyen_idler) * 3)
    if len(komsu_ids) > max_komsu:
        komsu_ids = set(random.sample(list(komsu_ids), max_komsu))

    model, builder = _model_kur(
        tasks, kilitler,
        ogretmen_listesi, ogretmen_izinleri,
        sinif_listesi, sinif_izinleri,
        yogunluk_ref=yogunluk_ref, soft=True,
        gelismis_ayarlar=gelismis_ayarlar
    )

    for t in tasks:
        tid = t.id
        presence = builder.task_vars[tid].get("presence")
        if tid in komsu_ids:
            if presence is not None:
                model.AddHint(presence, 1)
            continue
        if tid in yerlesmeyen_idler:
            if presence is not None:
                model.AddHint(presence, 1)
            hour_mid = max(0, (SAAT_SAYISI - t.blok) // 2)
            model.AddHint(builder.task_vars[tid]["hour"], hour_mid)
            continue
        try:
            sv = mevcut_solver.Value(mevcut_builder.task_vars[tid]["start"])
            model.AddHint(builder.task_vars[tid]["start"], sv)
            model.AddHint(builder.task_vars[tid]["day"],   sv // SAAT_SAYISI)
            model.AddHint(builder.task_vars[tid]["hour"],  sv %  SAAT_SAYISI)
            if presence is not None:
                model.AddHint(presence, 1)
        except Exception as e:
            log.debug(f"  [LNS] sabit hint hata tid={tid}: {e}")

    solver = _solver_parametreleri(sure_limit, verbose=False)
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        yeni = _yerlesmeyen_bul(solver, builder, tasks)
        if len(yeni) < len(yerlesmeyen_idler):
            return solver, builder, yeni
    return None, None, set(yerlesmeyen_idler)


def _asama_1_hard(tasks, kilitler, ogretmen_listesi, ogretmen_izinleri,
                  sinif_listesi, sinif_izinleri, sure: float,
                  greedy_hints: Dict[int, int],
                  gelismis_ayarlar: Optional[Dict] = None) -> PhaseResult:
    log.info(f"\n  ── AŞAMA 1 — HARD (Greedy hint'li) : {sure:.0f}s ──")
    model, builder = _model_kur(
        tasks, kilitler,
        ogretmen_listesi, ogretmen_izinleri,
        sinif_listesi, sinif_izinleri,
        soft=False, greedy_hints=greedy_hints,
        ardisiklik_aktif=True, gelismis_ayarlar=gelismis_ayarlar
    )
    solver = _solver_parametreleri(sure, verbose=False)
    t0 = time.time()
    status = solver.Solve(model)
    elapsed = time.time() - t0
    ds = _durum_str(status)
    log.info(f"  Aşama 1 tamamlandı: {ds} ({elapsed:.1f}s)")
    return PhaseResult(status=status, sure=elapsed, durum_str=ds,
                       solver=solver, builder=builder)


def _asama_2_soft(tasks, kilitler, ogretmen_listesi, ogretmen_izinleri,
                  sinif_listesi, sinif_izinleri, sure: float,
                  yogunluk_ref: Dict,
                  phase1_hints: Optional[Dict[int, int]],
                  greedy_hints: Optional[Dict[int, int]],
                  gelismis_ayarlar: Optional[Dict] = None) -> PhaseResult:
    log.info(f"\n  ── AŞAMA 2 — SOFT : {sure:.0f}s ──")
    hints = phase1_hints if phase1_hints else greedy_hints
    model, builder = _model_kur(
        tasks, kilitler,
        ogretmen_listesi, ogretmen_izinleri,
        sinif_listesi, sinif_izinleri,
        yogunluk_ref=yogunluk_ref, soft=True, greedy_hints=hints,
        ardisiklik_aktif=True, gelismis_ayarlar=gelismis_ayarlar
    )
    solver = _solver_parametreleri(sure, verbose=False)
    t0 = time.time()
    status = solver.Solve(model)
    elapsed = time.time() - t0
    ds = _durum_str(status)
    log.info(f"  Aşama 2 tamamlandı: {ds} ({elapsed:.1f}s)")

    yerl: Set[int] = set()
    yerl_adet = 0
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        yerl = _yerlesmeyen_bul(solver, builder, tasks)
        yerl_adet = len(tasks) - len(yerl)
    return PhaseResult(status=status, sure=elapsed, durum_str=ds,
                       solver=solver, builder=builder,
                       yerlesen=yerl_adet, yerlesmeyen=yerl)


def _asama_3_repair(tasks, kilitler, ogretmen_listesi, ogretmen_izinleri,
                    sinif_listesi, sinif_izinleri, sure: float,
                    phase2: PhaseResult,
                    gelismis_ayarlar: Optional[Dict] = None) -> PhaseResult:
    log.info(f"\n  ── AŞAMA 3 — REPAIR + LNS : {sure:.0f}s ──")

    best_solver  = phase2.solver
    best_builder = phase2.builder
    best_yerl    = set(phase2.yerlesmeyen)
    yogunluk_ref = phase2.builder._yogunluk

    repair_sure = sure * (1.0 - SURE_REPAIR_LNS_PAY)
    if repair_sure >= 2.0:
        log.info(f"  [Repair-Standart] {repair_sure:.0f}s")
        model, builder = _model_kur(
            tasks, kilitler,
            ogretmen_listesi, ogretmen_izinleri,
            sinif_listesi, sinif_izinleri,
            yogunluk_ref=yogunluk_ref, soft=True,
            onceki_solver=phase2.solver,
            onceki_task_vars=phase2.builder.task_vars,
            yerlesmeyen_idler=phase2.yerlesmeyen,
            ardisiklik_aktif=True, gelismis_ayarlar=gelismis_ayarlar
        )
        solver = _solver_parametreleri(repair_sure, verbose=False)
        t0 = time.time()
        status = solver.Solve(model)
        rep_elapsed = time.time() - t0
        log.info(f"  Repair-Standart: {_durum_str(status)} ({rep_elapsed:.1f}s)")
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            yeni = _yerlesmeyen_bul(solver, builder, tasks)
            if len(yeni) < len(best_yerl):
                log.info(f"  [Repair-Standart] Kazanım: {len(best_yerl)} → {len(yeni)}")
                best_solver, best_builder, best_yerl = solver, builder, yeni

    lns_sure_toplam = sure * SURE_REPAIR_LNS_PAY
    if lns_sure_toplam < 2.0 or not best_yerl:
        return PhaseResult(
            status=cp_model.FEASIBLE,
            sure=sure, durum_str="REPAIR_LNS",
            solver=best_solver, builder=best_builder,
            yerlesen=len(tasks) - len(best_yerl),
            yerlesmeyen=best_yerl,
        )

    log.info(f"  [LNS] Toplam {lns_sure_toplam:.0f}s için LNS turları başlıyor...")
    lns_tur_sure = max(4.0, min(lns_sure_toplam / LNS_MAX_DENEME, 15.0))
    t_start = time.time()
    deneme = 0
    while best_yerl and deneme < LNS_MAX_DENEME:
        kalan = lns_sure_toplam - (time.time() - t_start)
        if kalan < 2.0:
            break
        tur_sure = min(lns_tur_sure, kalan)
        sv, bd, yeni = _lns_swap_repair(
            best_solver, best_builder, tasks, best_yerl,
            tur_sure,
            ogretmen_izinleri, sinif_izinleri, kilitler,
            ogretmen_listesi, sinif_listesi,
            yogunluk_ref, gelismis_ayarlar=gelismis_ayarlar
        )
        deneme += 1
        if sv is not None and len(yeni) < len(best_yerl):
            log.info(f"  [LNS #{deneme}] Kazanım: {len(best_yerl)} → {len(yeni)}")
            best_solver, best_builder, best_yerl = sv, bd, yeni
        else:
            log.info(f"  [LNS #{deneme}] Gelişme yok.")

    return PhaseResult(
        status=cp_model.FEASIBLE,
        sure=time.time() - t_start,
        durum_str="REPAIR_LNS",
        solver=best_solver, builder=best_builder,
        yerlesen=len(tasks) - len(best_yerl),
        yerlesmeyen=best_yerl,
    )


# ════════════════════════════════════════════════════════════════════════════
#  ANA ORKESTRATÖR — program_hesapla (v9.0.1)
# ════════════════════════════════════════════════════════════════════════════

def program_hesapla(
    df_ders_yuku,
    sinif_listesi     : List[str],
    ogretmen_listesi  : List[str],
    ogretmen_izinleri : Dict,
    sinif_izinleri    : Dict,
    sure_saniye       : float = 600.0,
    kilitli_saatler   : Optional[List[Dict]] = None,
    gelismis_ayarlar  : Optional[Dict] = None,
    zaman_ayarlari    : Optional[Dict] = None, # <--- EKLENDİ
) -> Dict:
    """
    3-Aşamalı (Hard + Soft + Repair+LNS) ders programı motoru v9.0.1.
    """
    # <--- YENİ EKLENEN KISIM: Sabitleri Dinamik Hale Getirme --->
    global GUN_SAYISI, SAAT_SAYISI, HORIZON
    z_ayar = zaman_ayarlari or {}
    GUN_SAYISI = len(z_ayar.get("gunler") or ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma"])
    SAAT_SAYISI = int(z_ayar.get("ders_sayisi") or 8)
    HORIZON = GUN_SAYISI * SAAT_SAYISI
    # <----------------------------------------------------------->
    
    if not ORTOOLS_VAR:
        return {
            "durum": "HATA", "program": None, "kalan": [],
            "hata_mesaji": "OR-Tools kurulu değil: pip install ortools",
            "motor_durum": "OR_TOOLS_MISSING",
        }

    try:
        gelismis_ayarlar = gelismis_ayarlar or {}
        # ─── Her çalıştırmada farklı program için zaman tabanlı seed ──────
        random.seed(int(time.time() * 1000) % (2 ** 31))

        sure_saniye = max(5.0, float(sure_saniye))

        # ─── İzin verisi normalizasyonu ───────────────────────────────────
        # API [[saat, gun], ...] listesi gönderebilir; tüm motorda
        # (saat, gun) tuple bekleniyor (set() içinde hashlenebilmesi için).
        ogretmen_izinleri = {
            o: [tuple(x) for x in v]
            for o, v in (ogretmen_izinleri or {}).items()
        }
        sinif_izinleri = {
            c: [tuple(x) for x in v]
            for c, v in (sinif_izinleri or {}).items()
        }

        # ─── Veri hazırlığı ───────────────────────────────────────────────
        try:
            df, c0, c1, c2, c3 = _df_normalize(df_ders_yuku)
        except ValueError as e:
            return {
                "durum": "HATA", "program": None, "kalan": [],
                "hata_mesaji": str(e), "motor_durum": "INVALID_DATA",
            }
        tasks = _gorev_olustur(df, c0, c1, c2, c3, blok_stratejisi=gelismis_ayarlar.get("blok_stratejisi", "ikili"))
        kilitler = _kilitli_saatler_isle(kilitli_saatler)
        if not tasks:
            return {
                "durum": "HATA", "program": None, "kalan": [],
                "hata_mesaji": "Hiç görev oluşturulamadı (boş / hatalı ders yükü).",
                "motor_durum": "NO_TASKS",
            }

        log.info(f"\n{'═'*62}")
        log.info(f"  Motor v9.0.1 (Greedy + CP-SAT + LNS) başlıyor")
        log.info(f"  Görev: {len(tasks)}  |  Sınıf: {len(sinif_listesi)}  |  "
                 f"Öğretmen: {len(ogretmen_listesi)}")
        log.info(f"  CPU: {os.cpu_count()}  |  Süre: {sure_saniye:.0f}s  |  "
                 f"Kilit: {len(kilitler)}")
        log.info(f"{'═'*62}")

        # ─── Pre-Check ────────────────────────────────────────────────────
        checker = EnginePreCheck(tasks, ogretmen_izinleri, sinif_izinleri, kilitler)
        checker.kontrol_et()

        # ─── Süre dağılımı ───────────────────────────────────────────────
        # Tüm bütçe kullanılır: Hard → Soft → Repair+LNS
        sure_hard   = max(SURE_HARD_MIN, sure_saniye * SURE_ORAN_HARD)
        sure_soft   = sure_saniye * SURE_ORAN_SOFT
        sure_repair = sure_saniye * SURE_ORAN_REPAIR
        toplam = sure_hard + sure_soft + sure_repair
        if toplam > sure_saniye:
            # SURE_HARD_MIN nedeniyle aşma olursa önce repair, sonra soft kısılır
            fazla = toplam - sure_saniye
            sure_repair = max(1.0, sure_repair - fazla)
            fazla2 = (sure_hard + sure_soft + sure_repair) - sure_saniye
            if fazla2 > 0:
                sure_soft = max(5.0, sure_soft - fazla2)
        log.info(f"\n  Süre Dağılımı: Hard={sure_hard:.0f}s, "
                 f"Soft={sure_soft:.0f}s, Repair={sure_repair:.0f}s")

        # ─── GREEDY WARM-START ───────────────────────────────────────────
        log.info("\n  ── GREEDY WARM-START ──")
        greedy = GreedyInitialSolver(tasks, ogretmen_izinleri,
                                     sinif_izinleri, kilitler, gelismis_ayarlar=gelismis_ayarlar)
        greedy_starts = greedy.solve()
        log.info(f"  Greedy: {len(greedy_starts)}/{len(tasks)} görev yerleştirildi.")

        # ─── AŞAMA 1 — HARD ──────────────────────────────────────────────
        p1 = _asama_1_hard(tasks, kilitler,
                           ogretmen_listesi, ogretmen_izinleri,
                           sinif_listesi, sinif_izinleri,
                           sure_hard, greedy_starts, gelismis_ayarlar=gelismis_ayarlar)

        # Aşama 1'in yoğunluk referansını sonraki aşamalara aktar
        yogunluk_ref: Dict[str, float] = p1.builder._yogunluk if p1.builder else {}

        # Aşama 1'de bulunan çözüm varsa hint olarak topla
        phase1_hints: Dict[int, int] = {}
        if p1.status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for t in tasks:
                try:
                    phase1_hints[t.id] = p1.solver.Value(
                        p1.builder.task_vars[t.id]["start"])
                except Exception as e:
                    log.debug(f"  phase1 hint oku hata tid={t.id}: {e}")
            log.info(f"  {len(phase1_hints)} yerleşim ipucu Aşama 2'ye aktarılıyor.")

        # ─── AŞAMA 2 — SOFT ──────────────────────────────────────────────
        # NOT: Aşama 1 OPTIMAL olsa bile Aşama 2 çalıştırılır; böylece
        # hem tam süre kullanılır hem her çalıştırmada farklı program üretilir.
        p2 = _asama_2_soft(tasks, kilitler,
                           ogretmen_listesi, ogretmen_izinleri,
                           sinif_listesi, sinif_izinleri,
                           sure_soft, yogunluk_ref,
                           phase1_hints if phase1_hints else None,
                           greedy_starts, gelismis_ayarlar=gelismis_ayarlar)

        reporter2 = ResultReporter(
            p2.solver, p2.builder.task_vars, tasks,
            sinif_listesi, ogretmen_izinleri, sinif_izinleri, kilitler,
            sure_saniye=sure_saniye,
            gelismis_ayarlar=gelismis_ayarlar,  # FIX: HATA-2a
        )

        if p2.status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            log.warning(f"  Aşama 2 sonuç üretemedi ({p2.durum_str}).")
            # Aşama 1'de geçerli çözüm varsa onu döndür
            if p1.status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                log.info("  Aşama 1'den kalan geçerli çözüm döndürülüyor.")
                reporter1 = ResultReporter(
                    p1.solver, p1.builder.task_vars, tasks,
                    sinif_listesi, ogretmen_izinleri, sinif_izinleri, kilitler,
                    sure_saniye=sure_saniye,
                    gelismis_ayarlar=gelismis_ayarlar,  # FIX: HATA-2b
                )
                return reporter1.sonuc_uret(p1.status, f"HARD/{p1.durum_str}", soft=False)
            # Hiç çözüm yoksa hata raporu
            temp_sonuc = reporter2.sonuc_uret(p2.status, f"SOFT/{p2.durum_str}", soft=True)
            if temp_sonuc.get("kalan"):
                analiz = ExpertReporter.analiz_et(
                    tasks, temp_sonuc["kalan"], ogretmen_izinleri, sinif_izinleri)
                log.info(analiz)
                # Kapasite uyarıları varsa analiz öncesine ekle
                mevcut = temp_sonuc.get("hata_mesaji", "")
                temp_sonuc["hata_mesaji"] = (mevcut + "\n\n" + analiz).strip() if mevcut else analiz
            log.info(f"{'═'*62}\n")
            return temp_sonuc

        log.info(
            f"  Aşama 2: {p2.yerlesen}/{len(tasks)} yerleşti, "
            f"{len(p2.yerlesmeyen)} dışarıda."
        )

        # Tüm görevler Aşama 2'de yerleştiyse Aşama 3 atlanır
        if not p2.yerlesmeyen:
            log.info("  ✓ Tüm görevler Aşama 2'de yerleşti, Aşama 3 atlanıyor.")
            sonuc = reporter2.sonuc_uret(p2.status, f"SOFT/{p2.durum_str}", soft=True)
            log.info(f"{'═'*62}\n")
            return sonuc

        # ─── AŞAMA 3 — REPAIR + LNS ──────────────────────────────────────
        p3 = _asama_3_repair(tasks, kilitler,
                             ogretmen_listesi, ogretmen_izinleri,
                             sinif_listesi, sinif_izinleri,
                             sure_repair, p2, gelismis_ayarlar=gelismis_ayarlar)

        reporter3 = ResultReporter(
            p3.solver, p3.builder.task_vars, tasks,
            sinif_listesi, ogretmen_izinleri, sinif_izinleri, kilitler,
            sure_saniye=sure_saniye,
            gelismis_ayarlar=gelismis_ayarlar,  # FIX: HATA-2c
        )
        sonuc = reporter3.sonuc_uret(cp_model.FEASIBLE, p3.durum_str, soft=True)

        if sonuc.get("kalan"):
            analiz = ExpertReporter.analiz_et(
                tasks, sonuc["kalan"], ogretmen_izinleri, sinif_izinleri)
            log.info(analiz)
            # Kapasite uyarıları varsa analiz öncesine ekle, üzerine yazma
            mevcut = sonuc.get("hata_mesaji", "")
            sonuc["hata_mesaji"] = (mevcut + "\n\n" + analiz).strip() if mevcut else analiz

        yerlesmeyen_saat = len(sonuc.get("kalan", []))
        log.info(
            f"\n  ═════ SONUÇ ═════\n"
            f"  Yerleşen görev: {len(tasks) - len(p3.yerlesmeyen)}/{len(tasks)}\n"
            f"  Havuzda: {yerlesmeyen_saat} saat (1 saatlik birimler)"
        )
        log.info(f"{'═'*62}\n")
        return sonuc

    except Exception:
        tb = traceback.format_exc()
        log.error(f"  [HATA] Beklenmedik istisna:\n{tb}")
        return {
            "durum": "HATA", "program": None, "kalan": [],
            "hata_mesaji": tb, "motor_durum": "EXCEPTION",
        }
