# Hermes Telegram Summarizer

Bot Telegram cloud-ready untuk menyimpan percakapan grup dan membuat ringkasan dengan Hermes melalui API yang kompatibel dengan OpenAI Chat Completions.

## Fitur

- Webhook Telegram, jadi laptop tidak perlu menyala.
- Login akun Telegram per pengguna dengan session terenkripsi.
- `/connect` lalu `/grup` untuk memilih grup tanpa menambahkan bot ke grup tersebut.
- `/summary_group` merangkum grup yang dipilih.
- Menyimpan pesan grup dan pesan privat ke SQLite.
- `/summary` merangkum 24 jam terakhir.
- `/summary 6` merangkum 6 jam terakhir.
- Menyaring basa-basi dan hanya mempertahankan berita, keputusan, tugas, tenggat, dan perubahan penting.
- Mode privat: forward pesan dari grup atau channel ke bot, lalu gunakan `/summary`; bot tidak perlu masuk grup tersebut.
- `/translate en teks` menerjemahkan ke Inggris; tersedia `id`, `en`, `de`, `fr`, dan `th`.
- `/help` menampilkan panduan command dan mode penggunaan.
- Target translate dapat berupa nama bahasa bebas, misalnya `Japanese`, `Arabic`, atau `Korean`.
- Hanya admin grup yang dapat meminta ringkasan.
- Bisa diarahkan ke Hermes lokal, gateway cloud, atau endpoint OpenAI-compatible lain lewat environment variables.

## Menjalankan lokal

1. Salin `.env.example` menjadi `.env`, lalu isi token Telegram dan konfigurasi Hermes.
2. Pasang dependency: `pip install -r requirements.txt`.
3. Jalankan: `uvicorn app:api --reload`.

Webhook membutuhkan URL HTTPS publik. Saat development, gunakan tunnel seperti Cloudflare Tunnel atau ngrok, lalu isi `WEBHOOK_URL` dengan URL tunnel.

## Deploy cloud

Deploy sebagai web service Docker. Set semua variable dari `.env.example` di dashboard provider, gunakan port `8000`, dan pasang persistent volume pada `/app/data` agar database tidak hilang saat container restart.

Setelah deploy, tambahkan bot ke grup dan matikan **Group Privacy** bot melalui BotFather agar bot dapat membaca pesan biasa. Bot hanya menyimpan teks yang dikirim setelah bot aktif.

Untuk mode privat, buka chat bot lalu forward pesan yang ingin dipantau. Kirim `/summary` di chat bot. Telegram tidak mengizinkan bot membaca grup yang tidak diikutinya; forward adalah cara resmi tanpa memasukkan bot ke grup. Untuk menerjemahkan, kirim `/translate de teks` atau reply pesan dengan `/translate id`. Nama bahasa dapat ditulis bebas; hasil terbaik tetap bergantung pada kemampuan Hermes terhadap bahasa tersebut.

## Mode akun Telegram pribadi

Set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, dan `SESSION_ENCRYPTION_KEY` sebagai secret cloud. Di chat privat bot, jalankan `/connect`, buka link HTTPS yang diberikan, lalu masukkan nomor, kode login, dan password 2FA hanya di halaman tersebut. Setelah terhubung, jalankan `/grup`, pilih tombol grup, lalu gunakan `/summary_group` atau `/summary_group en`. Session setiap pengguna disimpan terenkripsi; jangan mengirim kode OTP atau session ke siapa pun.

Pasang Railway Volume dengan mount path `/app/data`. Tanpa Volume, database dan session bisa hilang setiap redeploy sehingga pengguna diminta login lagi. Jangan mengubah `SESSION_ENCRYPTION_KEY` setelah session tersimpan.

## Keamanan

Jangan commit `.env`. Gunakan `WEBHOOK_SECRET` untuk memvalidasi request Telegram dan simpan `TELEGRAM_BOT_TOKEN` serta `HERMES_API_KEY` sebagai secret cloud.
