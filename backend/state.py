"""
Enderun AI - Merkezi Uygulama State
Thread-safe singleton. Windows encoding-safe.
"""
import threading
import re as _re

def _dogal_sirala(s):
    """Natural sort key: 9A < 10A < 11B gibi."""
    return [int(c) if c.isdigit() else c.lower() for c in _re.split(r'(\d+)', str(s))]

from datetime import datetime, timedelta
from typing import Any, Dict, List
import pandas as pd


def _sutun_bul(kolonlar, anahtar_kelimeler):
    """Sütun adını encoding'den ve Türkçe karakterden bağımsız bulur."""
    def _norm(s):
        return (str(s).lower()
                .replace("ı", "i").replace("ğ", "g").replace("ş", "s")
                .replace("ö", "o").replace("ü", "u").replace("ç", "c"))
    for kelime in anahtar_kelimeler:
        for k in kolonlar:
            if kelime in _norm(k):
                return k
    return kolonlar[0]


class AppState:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "ogretmen_listesi": [],
            "sinif_listesi":    [],
            "ders_listesi":     [],
            "ogretmen_izinleri": {},
            "sinif_izinleri":    {},
            "df_ders_yuku":     None,
            "zaman_ayarlari": {
                "baslama":           "09:15",
                "ders_dk":           40,
                "teneffus_dk":       10,
                "ogle_sonrasi_ders": 5,
                "ogle_dk":           50,
                "ozel_saatler":      {},
            },
            "aktif_saat_etiketleri": {str(i): f"{i}. Saat" for i in range(1, 9)},
            "uretilen_program":  None,
            "uretilen_kalan":    [],
            "motor_calisiyor":   False,
            "son_motor_sonuc":   None,
            "son_motor_hata":    "",
            "kilitli_slotlar":   {},
            "karma_oncesi_snapshot": None,
            "gelismis_ayarlar": {
                "blok_stratejisi": "ikili",  # "ikili" veya "tekli"
                "gun_yayilimi": True,
                "ardisiklik_boslugu": 1,     # 0, 1 veya 2
                "ogr_snf_gunluk_limit": True,
                "gunluk_max_blok": True,
                "yogun_ders_egilimi": "yok", # "yok", "son_saatler", "ilk_saatler"
                "yogun_ders_saat_esigi": 5,
                "ders_iliskileri": []
            },
        }

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value

    def update(self, mapping: dict):
        with self._lock:
            self._data.update(mapping)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def saatleri_hesapla(self):
        with self._lock:
            z = self._data.get("zaman_ayarlari") or {}
        try:
            fmt = "%H:%M"
            
            baslama_degeri = z.get("baslama")
            if not baslama_degeri or str(baslama_degeri).strip() == "None" or str(baslama_degeri).strip() == "":
                baslama_degeri = "09:15"
                
            su_an   = datetime.strptime(str(baslama_degeri).strip(), fmt)
            ders_dk = int(z.get("ders_dk") or 40)
            ten_dk  = int(z.get("teneffus_dk") or 10)
            ogle_dk = int(z.get("ogle_dk") or 50)
            ogle_no = int(z.get("ogle_sonrasi_ders") or 5)
            ozel    = z.get("ozel_saatler") or {}
            
            max_saat = int(z.get("ders_sayisi") or 8)

            etiketler = {}
            for i in range(1, max_saat + 1):
                k = str(i)
                ozel_deger = str(ozel.get(k, "")).strip()
                
                # Eğer geçerli bir manuel saat girildiyse (içinde : ve - varsa)
                if ozel_deger and ":" in ozel_deger and "-" in ozel_deger:
                    ozel_deger = ozel_deger.replace("\n", " ") # Eski alt satırları temizle
                    etiketler[k] = ozel_deger
                    ozel[k] = ozel_deger
                    try:
                        bitis_str = ozel_deger.split("-")[1].strip().replace(")", "")
                        su_an = datetime.strptime(bitis_str, fmt) + timedelta(
                            minutes=(ogle_dk if i == ogle_no else ten_dk)
                        )
                    except Exception:
                        pass
                else:
                    # YENİ HESAPLA (Alt satır \n KULLANMADAN YAN YANA YAZ!)
                    bitis = su_an + timedelta(minutes=ders_dk)
                    yeni_etiket = f"{i}. Saat ({su_an.strftime(fmt)} - {bitis.strftime(fmt)})"
                    etiketler[k] = yeni_etiket
                    ozel[k] = yeni_etiket # Bozulan arayüz kutucuklarını zorla onarmak için eklendi
                    
                    su_an = bitis + timedelta(
                        minutes=(ogle_dk if i == ogle_no else ten_dk)
                    )
        except Exception as e:
            print(f"❌ SAAT HESAPLAMA HATASI: {e}")
            max_saat = int(z.get("ders_sayisi") or 8)
            etiketler = {str(i): f"{i}. Saat" for i in range(1, max_saat + 1)}

        with self._lock:
            self._data["aktif_saat_etiketleri"] = etiketler
            # Arayüzdeki kutulara doğru metni zorla göndermek için:
            if "zaman_ayarlari" not in self._data:
                self._data["zaman_ayarlari"] = {}
            self._data["zaman_ayarlari"]["ozel_saatler"] = ozel

    def listeleri_guncelle(self):
        with self._lock:
            df  = self._data["df_ders_yuku"]
            ogr = set(self._data["ogretmen_listesi"])
            snf = set(self._data["sinif_listesi"])
            drs = set(self._data["ders_listesi"])

        if df is not None and not df.empty:
            cols = list(df.columns)
            c_snf  = _sutun_bul(cols, ["sinif", "sinif", "class"])
            c_ders = _sutun_bul(cols, ["ders", "lesson", "subject"])
            c_ogr  = _sutun_bul(cols, ["ogretmen", "teacher", "instructor"])
            c_saat = _sutun_bul(cols, ["saat", "hour"])

            for c in [c_snf, c_ders, c_ogr]:
                df[c] = df[c].astype(str).str.strip().str.replace(r'\s+', ' ', regex=True)
            df = df.dropna(subset=[c_snf, c_ders, c_ogr, c_saat])

            for v in df[c_ogr]:
                for o in v.split('+'): ogr.add(o.strip())
            for v in df[c_snf]:
                for s2 in v.split('+'): snf.add(s2.strip())
            for v in df[c_ders]:
                drs.add(v.strip())

        with self._lock:
            self._data["ogretmen_listesi"] = sorted(list(ogr - {'', 'nan'}), key=_dogal_sirala)
            self._data["sinif_listesi"]    = sorted(list(snf - {'', 'nan'}), key=_dogal_sirala)
            self._data["ders_listesi"]     = sorted(list(drs - {'', 'nan'}), key=_dogal_sirala)
            if df is not None:
                self._data["df_ders_yuku"] = df

    @staticmethod
    def sinif_parcala(snf_str: str):
        ilk  = snf_str.split('+')[0].strip()
        sayi = "".join(c for c in ilk if c.isdigit())
        harf = "".join(c for c in ilk if not c.isdigit() and not c.isspace()).upper()
        return (int(sayi) if sayi else 9999, harf)

    def havuzu_sirala(self):
        with self._lock:
            self._data["uretilen_kalan"].sort(
                key=lambda d: self.sinif_parcala(d.get("sinif", ""))
            )

    def ogretmen_musait_mi(self, ogretmen_str: str, gun: int, saat: int,
                    haric_snf_list: List[str]) -> bool:
        """
        Öğretmenin belirtilen gün/saatte müsait olup olmadığını kontrol eder.
        SADECE haric_snf_list dışındaki sınıflara bakar (slot bazlı kontrol).
        """
        if not ogretmen_str:
            return True
        with self._lock:
            prog_raw = self._data["uretilen_program"]
            siniflar = self._data["sinif_listesi"]
            ogr_izin = self._data["ogretmen_izinleri"]
            kilitli = self._data.get("kilitli_slotlar", {})

        # JSON round-trip sonrası string key olabilir — normalize et
        prog = prog_raw
        if prog_raw:
            ilk = next(iter(prog_raw))
            if isinstance(ilk, str):
                prog = {int(g): {int(s): v for s, v in sv.items()}
                    for g, sv in prog_raw.items()}

        for ogr in [o.strip() for o in ogretmen_str.split('+')]:
            # İzin kontrolü
            for iz in ogr_izin.get(ogr, []):
                if iz[0] == saat and iz[1] == gun:
                    return False
            
            # Program kontrolü - sadece haric_snf_list dışındaki sınıflar
            if prog:
                for snf in siniflar:
                    if snf in haric_snf_list:
                        continue
                    d = prog[gun][saat].get(snf)
                    if d and ogr in d.get("ogretmen", ""):
                        return False
            
            # Kilit kontrolü - haric_snf_list dışındaki kilitler
            for key, lock_data in kilitli.items():
                if lock_data.get("gun") == gun and lock_data.get("saat") == saat:
                    lock_siniflar = lock_data.get("siniflar", [lock_data.get("isim")])
                    if any(ls not in haric_snf_list for ls in lock_siniflar):
                        if ogr in lock_data.get("ogretmen", ""):
                            return False
        return True
    
    def slot_kilitli_mi(self, sinif: str, gun: int, saat: int) -> bool:
        """
        Belirtilen (sınıf, gün, saat) slotunun kilitli olup olmadığını kontrol eder.
        """
        with self._lock:
            kilitli = self._data.get("kilitli_slotlar", {})
        
        # Key format: "sinif:SINIF_ADI:gun_saat"
        key = f"sinif:{sinif}:{gun}_{saat}"
        return key in kilitli


state = AppState()