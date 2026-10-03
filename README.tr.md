# social-video-qa

Videonuzu yüklemeden önce platformun kurallarına göre denetleyen, düzeltilebilecek sorunları da güvenle düzelten bir
komut satırı aracı. Instagram (Graph API), Meta reklamları, App Store uygulama önizlemeleri, YouTube Shorts ve TikTok
için hazır profillerle gelir. İnternete bağlanmaz, yalnızca FFmpeg kullanır.

Ayrıntılı belge İngilizce: [README.md](README.md)

## Ne işe yarar?

- **Ret gelmeden yakalar.** `moov` kutusunun yeri, düzenleme listeleri (edit list), kare boyutu, süre, kare hızı,
  kodek, profil ve seviye, bit hızı, ses biçimi.
- **Sessizce bozulanı da gösterir.** Akışta kırpılacak en boy oranı, sonradan sesi kaydıran değişken kare hızı, fazla
  yüksek ya da cılız ses, kapak olacak siyah ilk kare, videoda unutulan konum bilgisi.
- **Düzeltirken en ucuz yolu seçer.** Yetiyorsa yalnızca kutuların sırasını düzeltir; gerekirse yalnızca sesi, şart
  olduğunda görüntüyü de yeniden kodlar. Sonucu yeniden denetler, sorun yoksa yerine koyar. `--apply` vermezseniz
  hiçbir şey yazmaz, yalnızca planı gösterir.

## Kurulum

Adım adım rehber İngilizce: [docs/setup.md](docs/setup.md). Fedora, eski dağıtımlar, root yetkisi olmayan sunucular ve
CI da orada anlatılıyor. Kısaca:

1. **FFmpeg'i kurun** (4.4 ya da üstü, libx264 ile birlikte):

   ```sh
   brew install ffmpeg            # macOS
   sudo apt install ffmpeg        # Debian / Ubuntu
   ```

   Fedora'nın kendi `ffmpeg-free` paketinde libx264 bulunmuyor, bu yüzden RPM Fusion'daki `ffmpeg` paketini kurun.
   Ubuntu 20.04 ve Debian 11'deki FFmpeg sürümü eski kalıyor. Bu sistemlerde ffmpeg.org'dan hazır (statik) bir derleme
   indirip yerini `SVQA_FFMPEG` ve `SVQA_FFPROBE` değişkenleriyle gösterin.

2. **svqa'yı kurun.** Python 3.10 ya da üstü gerekir (`python3 --version` ile bakabilirsiniz). svqa GitHub'dan git
   ile indirildiği için git de kurulu olmalı:

   ```sh
   pipx install git+https://github.com/deegitech/social-video-qa
   ```

   `pipx` yoksa önce `brew install pipx` ya da `sudo apt install pipx`, ardından `pipx ensurepath` çalıştırıp yeni bir
   terminal açın. Makinede git yoksa sürüm arşivinden kurabilirsiniz:
   `pipx install https://github.com/deegitech/social-video-qa/archive/refs/tags/v0.1.0.tar.gz`. Ubuntu 20.04 ve
   Debian 11'deki Python da eski kalıyor; root yetkisi olmadan uv ile kurmanın yolu rehberde anlatılıyor. Sistemin
   Python'una `pip install` ile kurmaya kalkarsanız `externally-managed-environment` hatası alırsınız. Bu beklenen bir
   durum: pipx ya da bir sanal ortam (venv) kullanın.

3. **`svqa doctor` çalıştırın.** FFmpeg'in yerini ve sürümünü, gereken filtreleri ve kodlayıcıları, profillerinizi tek
   tek denetler, yarım saniyelik bir deneme kodlaması da yapar. Her satırın başında bir işaret görürsünüz: ✓ hazır,
   ✗ sorun var (altında ne yapmanız gerektiği yazar), `!` çalışıyor ama bir göz atın, `-` isteğe bağlı ve bu makinede
   kullanılmıyor. Hazır bir makinede de `-` satırları çıkabilir. Her şey hazırsa 0, değilse 1 koduyla çıkar, yani
   betiklerde ve CI'da `svqa doctor || exit 1` diye kullanılabilir. Hiçbir dosya yazmaz, internete çıkmaz.

4. **İlk deneme.** Elinizde video yoksa FFmpeg 12 saniyelik bir deneme klibi üretebilir:

   ```sh
   ffmpeg -f lavfi -i testsrc2=size=1080x1920:rate=30:duration=12 \
          -f lavfi -i sine=frequency=440:sample_rate=48000:duration=12 \
          -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest klip.mp4
   ```

   Önce denetleyin, sonra düzeltme planına bakın. `--apply` vermediğiniz sürece hiçbir dosya yazılmaz.

   ```sh
   svqa check klip.mp4
   svqa fix klip.mp4 -p instagram-reels-api            # yalnızca planı gösterir
   svqa fix klip.mp4 -p instagram-reels-api --apply    # klip.instagram-reels-api.mp4 dosyasını yazar
   ```

   Platforma orijinal dosyayı değil, `klip.instagram-reels-api.mp4` dosyasını yükleyin.

Hesap, API anahtarı ya da token gerekmez. Bir hata mı aldınız? Çoğu hata mesajının altında `hint:` ile başlayan tek
satırlık bir çözüm önerisi çıkar. Bütün mesajların anlamı ve çözümü
[docs/troubleshooting.md](docs/troubleshooting.md) dosyasında.

## Kullanım

```sh
svqa check klip.mp4                                   # hangi platforma hazır?
svqa check klip.mp4 -p instagram-reels-api            # tek platform, ayrıntılı rapor
svqa fix klip.mp4 -p app-store-preview-iphone         # düzeltme planı (hiçbir şey yazmaz)
svqa fix klip.mp4 -p app-store-preview-iphone --apply --trim
svqa check cikti/ -p tiktok --junit qa.xml            # CI için JUnit raporu
```

Kendi kurallarınızı JSON, YAML ya da TOML dosyasıyla yazabilir, hazır bir profili genişletip yalnızca farklı olanı
değiştirebilirsiniz ([docs/writing-profiles.md](docs/writing-profiles.md)).

## Güvenlik

- Ağa çıkmaz; hesap, anahtar ya da parola istemez.
- FFmpeg'i kabuk kullanmadan, yalnızca yerel dosya ve bilinen kap biçimleriyle çalıştırır. Video kılığına girmiş
  oynatma listeleri ve birleştirme betikleri reddedilir.
- Girdiye asla yazmaz. Çıktıyı önce geçici dosyaya yazar, denetler, sonra tek hamlede yerine koyar. Yazdığı dosyaları
  özetleriyle bir günlükte (`.svqa-manifest.json`) tutar; kendi yazmadığı dosyanın üzerine yazmaz.
- `svqa fix` varsayılan olarak konum, cihaz ve tarih gibi üst verileri siler.

## Bilinmesi gerekenler

- Platformlar kurallarını haber vermeden değiştirir. Her kuralın kaynağı ve kontrol tarihi
  [docs/profiles.md](docs/profiles.md) dosyasında; resmî belgede olmayıp pratikte gördüğümüz kurallar `observed` diye
  işaretli.
- Hışırtı ölçüsü (hiss index) bir standart değil, deneyime dayalı bir göstergedir.
- `svqa fix` yalnızca MP4 içinde H.264 + AAC üretir; hışırtıyı temizlemez, kısa videoyu uzatmaz.
- HDR videolar (iPhone varsayılan olarak HLG HDR kaydeder) ton eşleme yapılmadan 8 bite çevrilir, renkler bozulabilir.
  `svqa fix` öncesinde kurgu programınızdan SDR olarak dışa aktarın.

## Hakkında

[DEEGITECH](https://github.com/deegitech) (DEEGITECH Teknoloji ve Yazılım Ltd. Şti.) tarafından geliştirilir. Nasıl
ortaya çıktığı İngilizce README'nin "About" bölümünde. Lisans: [MIT](LICENSE).
