"""
Kariyer Kapısı (kariyerkapisi.gov.tr/isealim) ilan takipçisi  -  v3

1) Ana sayfadaki tüm aktif ilanları bulur (sayfalama varsa sayfaları da gezer).
2) Her ilanın "Başvur" butonuna basıp açılan sayfayı okur (pozisyon bölümleri dahil).
3) Sayfada anahtar kelimelerden biri geçerse masaüstü bildirimi gönderir.

Kurulum:
    python -m pip install playwright plyer
    python -m playwright install chromium

Çalıştırma:
    python kariyerkapisi_takip.py          # sürekli çalışır
    python kariyerkapisi_takip.py --once   # bir kez kontrol edip çıkar
    python kariyerkapisi_takip.py --show   # tarayıcı penceresini göstererek çalışır (sorun giderme)
    python kariyerkapisi_takip.py --ayrinti  # tüm ayrıntıları yazar (sorun giderme)
    python kariyerkapisi_takip.py --kur    # arka planda çalıştır + bilgisayar açılınca otomatik başlat
    python kariyerkapisi_takip.py --kaldir # otomatik başlatmayı kaldır
    python kariyerkapisi_takip.py --test   # test bildirimi gönderir
    python kariyerkapisi_takip.py --reset  # görülen ilan kaydını sıfırlar
"""

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

# ---------------------------- AYARLAR ----------------------------
URL = "https://kariyerkapisi.gov.tr/isealim"
ANAHTAR_KELIMELER = [
    "tekniker",
    "bilgisayar",
    "bilgisayar programcılığı",
    "bilgisayar teknikeri",
    "bilgi işlem",
]
KONTROL_ARALIGI_DK = 30
# Telefon bildirimi (ntfy.sh). Telefonda ntfy uygulamasını kurup bu konuya abone ol.
# Konu adı gizli bir şifre gibidir, kimseyle paylaşma. Kapatmak için "" yap.
NTFY_KONU = os.environ.get("NTFY_KONU", "").strip()  # GitHub Secret olarak verilir, dosyada yazmaz
NTFY_SUNUCU = "https://ntfy.sh"
MAX_SAYFA = 30  # listede en fazla kaç sayfa gezilsin
KLASOR = Path(__file__).resolve().parent
DURUM_DOSYASI = KLASOR / "gorulen_ilanlar.json"
# -----------------------------------------------------------------

_TR = str.maketrans("İIıÖöÜüŞşÇçĞğ", "iiioouussccgg")
SONRAKI_SECICI = (
    "a[aria-label='Next'], a[aria-label='Sonraki'], li.next:not(.disabled) > a, "
    ".paginate_button.next:not(.disabled), a:has-text('Sonraki'), button:has-text('Sonraki'), "
    "a:has-text('›'), a:has-text('»')"
)
ACILIR_SECICI = (
    "[aria-expanded='false'], [data-toggle='collapse'], [data-bs-toggle='collapse'], .accordion-button"
)


def normalize(s: str) -> str:
    """Türkçe karakter ve büyük/küçük harf farkını yok say (uzunluk korunur)."""
    return s.translate(_TR).lower()


def tek_satir(s: str) -> str:
    return " ".join(s.split())


VERBOSE = False


LOG_DOSYASI = KLASOR / "takip.log"


def log(msg: str) -> None:
    satir = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(satir, flush=True)  # arka planda (pythonw) konsol yok, sorun olmaz
    except Exception:
        pass
    try:
        if LOG_DOSYASI.exists() and LOG_DOSYASI.stat().st_size > 500_000:
            LOG_DOSYASI.write_text("", encoding="utf-8")
        with open(LOG_DOSYASI, "a", encoding="utf-8") as f:
            f.write(satir + "\n")
    except Exception:
        pass


def dlog(msg: str) -> None:
    """Sadece --ayrinti ile görünen ayrıntılı mesajlar."""
    if VERBOSE:
        log(msg)


def telefona_gonder(baslik: str, mesaj: str) -> None:
    if not NTFY_KONU:
        return
    try:
        veri = json.dumps(
            {
                "topic": NTFY_KONU,
                "title": baslik,
                "message": mesaj,
                "priority": 4,
                "tags": ["bell"],
                "click": URL,
            }
        ).encode("utf-8")
        istek = urllib.request.Request(NTFY_SUNUCU, data=veri, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(istek, timeout=15).read()
    except Exception as e:
        log(f"Telefon bildirimi gönderilemedi: {e}")


def bildirim(baslik: str, mesaj: str) -> None:
    telefona_gonder(baslik, mesaj)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return  # bulutta masaüstü yok
    try:
        from plyer import notification

        notification.notify(
            title=baslik[:60],
            message=mesaj[:240],
            app_name="Kariyer Kapısı Takip",
            timeout=20,
        )
    except Exception as e:
        log(f"Masaüstü bildirimi gönderilemedi ({e}); sadece konsola yazıldı.")
        try:
            import winsound

            winsound.MessageBeep()
        except Exception:
            print("\a")


def eslesmeleri_bul(metin: str) -> tuple[list[str], str]:
    n = normalize(metin)
    bulunan, ilk_index = [], None
    for k in ANAHTAR_KELIMELER:
        i = n.find(normalize(k))
        if i != -1:
            bulunan.append(k)
            if ilk_index is None or i < ilk_index:
                ilk_index = i
    alinti = ""
    if ilk_index is not None:
        alinti = tek_satir(metin[max(0, ilk_index - 60): ilk_index + 100])
    return bulunan, alinti


# ------------------------- Tarayıcı işlemleri -------------------------
def basvur_butonlari(page):
    """Sayfadaki 'Başvur' butonlarını bulur (tam eşleşme, yoksa gevşek eşleşme)."""
    adaylar = "a, button, input[type='button'], input[type='submit'], [role='button']"
    kesin = page.locator(adaylar).filter(has_text=re.compile(r"^\s*Başvur\s*$", re.I))
    if kesin.count() > 0:
        return kesin
    return page.locator(adaylar).filter(has_text=re.compile(r"Başvur(?!ul|um|u )", re.I))


def sonraki_sayfa(page) -> bool:
    """Listede 'sonraki sayfa' varsa ona geçer."""
    try:
        btn = page.locator(SONRAKI_SECICI).first
        if btn.count() == 0 or not btn.is_visible():
            return False
        sinif = (btn.get_attribute("class") or "") + (btn.evaluate("e => e.parentElement.className") or "")
        if "disabled" in sinif or btn.get_attribute("aria-disabled") == "true":
            return False
        onceki = page.inner_text("body")
        btn.click(timeout=3000)
        page.wait_for_timeout(1500)
        return page.inner_text("body") != onceki
    except Exception:
        return False


def listeye_git(page, sayfa_no: int) -> None:
    page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
    try:
        # İlan listesi sayfa açıldıktan sonra yüklenir; 'Başvur' butonu görünene kadar bekle
        page.wait_for_selector("text=/Başvur/i", timeout=45_000)
    except Exception:
        pass
    page.wait_for_timeout(2500)
    for _ in range(sayfa_no):
        if not sonraki_sayfa(page):
            break


def hedefleri_topla(page) -> list[dict]:
    hedefler = []
    for sayfa_no in range(MAX_SAYFA):
        butonlar = basvur_butonlari(page)
        adet = butonlar.count()
        for i in range(adet):
            b = butonlar.nth(i)
            try:
                satir = tek_satir(
                    b.evaluate("e => (e.closest('tr, .card, li, .list-group-item, .row') || e.parentElement).innerText")
                )
            except Exception:
                satir = ""
            hedefler.append(
                {"sayfa": sayfa_no, "idx": i, "href": b.get_attribute("href"), "satir": satir}
            )
        if not sonraki_sayfa(page):
            break
    return hedefler


def sayfa_metni(detay) -> str:
    """Gizli (açılır) bölümler dahil tüm sayfa metni."""
    try:
        for el in detay.query_selector_all(ACILIR_SECICI)[:40]:
            try:
                el.click(timeout=500)
            except Exception:
                pass
        # Pozisyon / alt ilan bölümlerini açmayı dene (sayfadan ayrılırsak geri dön)
        for el in detay.query_selector_all("a, button")[:200]:
            try:
                t = (el.inner_text() or "").strip().lower()
                if len(t) < 40 and re.search(r"pozisyon|alt ilan|kadro", normalize(t)):
                    onceki = detay.url
                    el.click(timeout=500)
                    detay.wait_for_timeout(400)
                    if detay.url != onceki:
                        detay.go_back()
                        detay.wait_for_timeout(500)
            except Exception:
                pass
        detay.wait_for_timeout(500)
    except Exception:
        pass
    try:
        return detay.evaluate("document.body.textContent") or ""
    except Exception:
        return ""


def hata_ayiklama_kaydet(page) -> None:
    try:
        (KLASOR / "debug_liste.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(KLASOR / "debug_liste.png"), full_page=True)
        dlog(f"Hata ayıklama dosyaları kaydedildi: {KLASOR / 'debug_liste.html'} ve debug_liste.png")
    except Exception as e:
        log(f"Hata ayıklama dosyası kaydedilemedi: {e}")


def ilanlari_tara(gorunur: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright

    sonuc = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not gorunur)
        ctx = browser.new_context(
            locale="tr-TR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()

        # Sorun giderme: sayfanın arka planda yaptığı veri isteklerini kaydet
        bekleyen = {}

        def _istek(r):
            if r.resource_type in ("xhr", "fetch") and "google" not in r.url:
                bekleyen[id(r)] = f"{r.method} {r.url[:160]}"
                dlog(f"AĞ İSTEK {r.method} {r.url[:160]}")

        def _yanit(r):
            if r.request.resource_type in ("xhr", "fetch"):
                bekleyen.pop(id(r.request), None)
                dlog(f"AĞ {r.status} {r.request.method} {r.url[:160]}")

        def _hata(r):
            bekleyen.pop(id(r), None)
            dlog(f"AĞ HATA {r.method} {r.url[:160]} -> {r.failure}")

        page.on("request", _istek)
        page.on("response", _yanit)
        page.on("requestfailed", _hata)
        page.on("console", lambda m: dlog(f"KONSOL {m.type}: {m.text[:160]}") if m.type == "error" else None)

        try:
            listeye_git(page, 0)
        except Exception:
            hata_ayiklama_kaydet(page)
            raise

        hedefler = hedefleri_topla(page)
        dlog(f"Listede {len(hedefler)} 'Başvur' butonu bulundu.")
        if not hedefler:
            dlog("Hiç 'Başvur' butonu bulunamadı (ya ilan yok ya da sayfa yapısı farklı).")
            for b in bekleyen.values():
                dlog(f"BEKLEYEN istek (cevap gelmedi): {b}")
            if not bekleyen:
                dlog("Sayfa ilan verisi için hiç istek göndermedi.")
            hata_ayiklama_kaydet(page)
            browser.close()
            return []

        # Aynı adrese giden tekrarları ele
        gorulen_href = set()
        for h in hedefler:
            if h["href"] and h["href"] in gorulen_href:
                h["atla"] = True
            elif h["href"]:
                gorulen_href.add(h["href"])

        for n, h in enumerate(hedefler, 1):
            if h.get("atla"):
                continue
            detay = None
            try:
                href = h["href"]
                if href and not href.startswith(("javascript", "#")):
                    detay = ctx.new_page()
                    detay.goto(urljoin(URL, href), wait_until="domcontentloaded", timeout=90_000)
                else:
                    listeye_git(page, h["sayfa"])
                    btn = basvur_butonlari(page).nth(h["idx"])
                    try:
                        with ctx.expect_page(timeout=4000) as yeni:
                            btn.click()
                        detay = yeni.value
                        detay.wait_for_load_state("domcontentloaded")
                    except Exception:
                        detay = page  # aynı sekmede açıldı
                        btn.click()
                        page.wait_for_load_state("domcontentloaded")

                detay.wait_for_timeout(4000)
                url = detay.url
                if "giris.turkiye.gov.tr" in url or "e-devlet" in normalize(url):
                    dlog(f"UYARI: ilan {n} e-Devlet giriş sayfasına yönlendirdi; ilan metni okunamıyor.")
                metin = h["satir"] + "\n" + sayfa_metni(detay)
                kimlik = url + "|" + hashlib.sha1(h["satir"].encode()).hexdigest()[:10]
                sonuc.append({"id": kimlik, "baslik": h["satir"][:120] or f"İlan {n}", "url": url, "metin": metin})
                dlog(f"[{n}/{len(hedefler)}] okundu ({len(metin)} karakter): {h['satir'][:60]}")
            except Exception as e:
                dlog(f"İlan {n} okunamadı: {e}")
            finally:
                if detay is not None and detay is not page:
                    try:
                        detay.close()
                    except Exception:
                        pass

        browser.close()
    return sonuc


# ------------------------- Otomatik başlatma (Windows) -------------------------
def _startup_lnk() -> Path:
    return (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "KariyerKapisiTakip.lnk"
    )


def _ps(x) -> str:
    return str(x).replace("'", "''")


def otomatik_baslat_kur() -> None:
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    if not pyw.exists():
        pyw = py
    script = Path(__file__).resolve()
    lnk = _startup_lnk()
    komut = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath='%s';$s.Arguments='\"%s\"';$s.WorkingDirectory='%s';$s.WindowStyle=7;$s.Save()"
        % (_ps(lnk), _ps(pyw), _ps(script), _ps(script.parent))
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", komut], check=True)
    DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen([str(pyw), str(script)], creationflags=DETACHED, cwd=str(script.parent), close_fds=True)
    print("Tamam. Program şimdi arka planda başladı ve bilgisayar her açıldığında otomatik başlayacak.")
    print(f"Kayıt dosyası: {LOG_DOSYASI}")


def otomatik_baslat_kaldir() -> None:
    lnk = _startup_lnk()
    if lnk.exists():
        lnk.unlink()
        print("Otomatik başlatma kaldırıldı. Çalışan programı durdurmak için Görev Yöneticisi'nde 'pythonw.exe' işlemini sonlandır.")
    else:
        print("Otomatik başlatma zaten kurulu değil.")


_KILIT = None


def tek_kopya_kilidi() -> bool:
    """Aynı anda sadece bir kopya çalışsın."""
    global _KILIT
    try:
        _KILIT = socket.socket()
        _KILIT.bind(("127.0.0.1", 48213))
        return True
    except OSError:
        return False


# ------------------------- Durum ve ana akış -------------------------
def durum_yukle() -> set[str]:
    if DURUM_DOSYASI.exists():
        try:
            return set(json.loads(DURUM_DOSYASI.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def durum_kaydet(gorulen: set[str]) -> None:
    DURUM_DOSYASI.write_text(json.dumps(sorted(gorulen), ensure_ascii=False), encoding="utf-8")


def ilan_adi(satir: str) -> str:
    """Satırdaki 'Başvur' yazısını ve fazla boşlukları atıp ilan adını döndürür."""
    ad = re.sub(r"\s*Başvur\s*$", "", tek_satir(satir), flags=re.I).strip()
    return ad or "(ilan adı okunamadı)"


def kontrol_et(gorunur: bool = False) -> None:
    ilanlar = ilanlari_tara(gorunur)
    if not ilanlar:
        dlog("Taranacak ilan bulunamadı.")
        return

    gorulen = durum_yukle()
    yeni = 0
    for ilan in ilanlar:
        bulunan, alinti = eslesmeleri_bul(ilan["metin"])
        ad = ilan_adi(ilan["baslik"])
        if not bulunan:
            dlog(f"Eşleşme yok: {ad[:70]}")
            continue

        anahtar = hashlib.sha1((ilan["id"] + "|" + ",".join(sorted(bulunan))).encode()).hexdigest()
        if anahtar in gorulen:
            dlog(f"Zaten bildirildi: {ad[:70]}")
            continue
        gorulen.add(anahtar)
        yeni += 1
        log(f"Eşleşme bulundu: {ad}")
        bildirim("Eşleşme bulundu", ad)
        dlog(f"Eşleşen kelimeler: {', '.join(bulunan)} | Link: {ilan['url']}")

    durum_kaydet(gorulen)
    dlog(f"Kontrol tamam. {len(ilanlar)} ilan tarandı, {yeni} yeni eşleşme.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="bir kez kontrol et ve çık")
    ap.add_argument("--show", action="store_true", help="tarayıcıyı görünür aç (sorun giderme)")
    ap.add_argument("--kur", action="store_true", help="bilgisayar açılınca arka planda otomatik başlat")
    ap.add_argument("--kaldir", action="store_true", help="otomatik başlatmayı kaldır")
    ap.add_argument("--ayrinti", action="store_true", help="ayrıntılı çıktı göster")
    ap.add_argument("--test", action="store_true", help="test bildirimi gönder")
    ap.add_argument("--reset", action="store_true", help="görülen ilan kaydını sil")
    args = ap.parse_args()
    global VERBOSE
    VERBOSE = args.ayrinti

    if args.kur:
        otomatik_baslat_kur()
        return
    if args.kaldir:
        otomatik_baslat_kaldir()
        return
    if args.test:
        bildirim("Test", "Bildirimler çalışıyor.")
        log("Test bildirimi gönderildi.")
        return
    if args.reset:
        DURUM_DOSYASI.unlink(missing_ok=True)
        log("Kayıt sıfırlandı.")
        return

    if not args.once and not tek_kopya_kilidi():
        print("Program zaten çalışıyor (arka planda). İkinci kopya başlatılmadı.")
        return

    print(f"Takip çalışıyor (her {KONTROL_ARALIGI_DK} dk). Eşleşme olunca burada yazacak. Durdurmak için Ctrl+C.", flush=True)
    while True:
        bekle = KONTROL_ARALIGI_DK * 60
        try:
            kontrol_et(args.show)
        except Exception as e:
            log(f"Hata: {e}")
            bekle = 5 * 60  # internet/site sorunu olabilir, 5 dk sonra tekrar dene
        if args.once:
            break
        time.sleep(bekle)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
