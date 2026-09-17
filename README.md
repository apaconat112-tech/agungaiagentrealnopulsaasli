# Hermes Telegram Summarizer

Bot Telegram cloud-ready untuk menyimpan percakapan grup dan membuat ringkasan dengan Hermes melalui API yang kompatibel dengan OpenAI Chat Completions.

## Fitur

- Webhook Telegram, jadi laptop tidak perlu menyala.
- Menyimpan pesan grup ke SQLite.
- `/summary` merangkum 24 jam terakhir.
- `/summary 6` merangkum 6 jam terakhir.
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

## Keamanan

Jangan commit `.env`. Gunakan `WEBHOOK_SECRET` untuk memvalidasi request Telegram dan simpan `TELEGRAM_BOT_TOKEN` serta `HERMES_API_KEY` sebagai secret cloud.
