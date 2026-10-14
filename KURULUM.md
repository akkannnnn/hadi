# Enderun AI v2.0 — Kurulum & Kullanım Kılavuzu

## ✅ Gereksinimler

| Gereksinim | Sürüm |
|---|---|
| Python | 3.10 veya üzeri |
| İşletim Sistemi | Windows 10/11, macOS 10.13+, Ubuntu 20.04+ |
| RAM | Minimum 2 GB (4 GB önerilir) |
| İnternet | Yalnızca ilk kurulum için |

---

## 1. Kurulum (Tek Seferlik)

```bash
# Projeyi açın
cd enderun-ai

# Bağımlılıkları yükleyin
pip install -r requirements.txt
```

---

## 2. Çalıştırma

```bash
python app.py
```

Uygulama bir native pencerede açılır.  
**Tarayıcı gerekmez, internet bağlantısı gerekmez.**

---

## 3. Kullanım Adımları

### Adım 1 — Excel Yükle
Excel dosyanızın şu sütunları içermesi gerekir:
- `Sınıf` — Örn: `9A`, `9A+9B` (birleşik)
- `Ders Adı` — Örn: `Matematik`
- `Öğretmen` — Örn: `Ali Yılmaz`, `Ali Yılmaz+Mehmet Demir` (birleşik)
- `Haftalık Saat` — Sayısal değer

### Adım 2 — Kısıtlama Paneli
- Zaman çizelgesini ayarlayın (başlama saati, ders süresi, öğle arası)
- Öğretmen/Sınıf kapalı saatlerini kırmızıya tıklayarak işaretleyin
- **Kaydet** butonuna basın

### Adım 3 — Ders Yükü Paneli
- Satır ekle/düzenle/sil
- Bir sınıfın derslerini başka sınıfa kopyalama (Şablon)
- Toplu kopyala-yapıştır

### Adım 4 — Programı Oluştur
- OR-Tools CP-SAT algoritması çalışır (maks. 120 saniye)
- Tamamlanınca otomatik Düzenleme ekranına geçer

### Düzenleme (Tıkla-Takasla)
- Herhangi bir derse tıklayın → seçilir (mor çerçeve)
- Yeşil hücrelere tıklayın → takas yapılır
- Havuzdaki derse tıklayın → programa yerleştirin
- Seçili dersi havuza atmak için havuz kartına tıklayın

### Program Görüntüleyici
- Sınıf bazlı / Öğretmen bazlı / Genel matris görünümleri
- Müsait öğretmenler tablosu
- Yazdır butonu (tüm sayfalar)

---

## 4. Excel Çıktısı

Renkli veya siyah-beyaz olarak 7 sekme içerir:
1. **Sınıflar (Tekli)** — Her sınıf ayrı A4'te
2. **Öğretmenler (Tekli)** — Her öğretmen ayrı A4'te
3. **Sınıflar (4'lü Matris)** — A4'e 4 sınıf
4. **Öğretmenler (4'lü Matris)** — A4'e 4 öğretmen
5. **Genel Sınıf** — Tüm sınıflar matris
6. **Genel Öğretmen** — Tüm öğretmenler matris
7. **Müsait Öğretmenler** — Her saat boş öğretmenler

---

## 5. Proje Kaydetme / Yükleme

- **Kaydet**: `.enderun` uzantılı dosyaya tüm veri kaydedilir
- **Yükle**: Eski `.enderun` dosyaları (Tkinter v1 dahil) desteklenir

---

## 6. .exe Oluşturma

```bash
python build.py
```

`dist/` klasöründe çalıştırılabilir oluşur:
- **Windows** → `EnderunAI.exe`
- **macOS** → `EnderunAI.app`
- **Linux** → `EnderunAI`

---

## 7. Sorun Giderme

### OR-Tools kurulmuyor
```bash
pip install --upgrade ortools
```

### PyWebView açılmıyor (Linux)
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.0
```

### Port 8742 kullanımda
`app.py` içindeki `PORT = 8742` satırını değiştirin.

### "Sunucuya bağlanılamadı" hatası
Başka bir `python app.py` çalışıyor olabilir. O işlemi kapatıp tekrar deneyin.

---

## 8. Teknik Detaylar

| Bileşen | Teknoloji |
|---|---|
| Optimizasyon Motoru | Google OR-Tools CP-SAT |
| Backend API | FastAPI + Uvicorn |
| Pencere | PyWebView (native WebView) |
| Excel Çıktısı | openpyxl |
| Veri İşleme | pandas |
| Paketleme | PyInstaller |
