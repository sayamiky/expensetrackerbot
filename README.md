# expense_track_bot

Bot Telegram pencatat pengeluaran. Kirim foto struk belanja atau tulis
pengeluaran ("makan siang 25rb"), lalu bot membacanya dengan OpenAI dan
menyimpannya ke Neon Postgres. Semua perhitungan "hari ini / minggu / bulan"
memakai zona waktu **WITA** (`Asia/Makassar`).

Stack: FastAPI (webhook) di Vercel · OpenAI vision + structured output ·
SQLAlchemy async + asyncpg · Neon.

## Alur

```
Foto struk ─▶ webhook ─▶ cek secret, allowlist, rate limit, dedupe update_id
           ─▶ kirim "⏳ Membaca struk…"
           ─▶ download foto ─▶ OpenAI (json_schema strict) ─▶ validasi angka
           ─▶ simpan draft (pending) ─▶ edit pesan "⏳" jadi draft + tombol
              [✅ Simpan] [❌ Batal] [✏️ Ubah kategori]
Tombol    ─▶ callback_query ─▶ confirm / cancel / setcat ─▶ edit pesan
Teks      ─▶ OpenAI ─▶ expense_log  → langsung tersimpan + tombol [↩️ Batalkan]
                     ─▶ summary_query → total dari database (bukan dari model)
                     ─▶ chat          → balasan singkat
/command  ─▶ tanpa OpenAI
```

Validasi sebelum menyimpan (`app/services/expense_check.py`):
- jumlah item ≈ subtotal dan subtotal − diskon + pajak ≈ total (selisih
  ditampilkan sebagai ⚠️ di draft)
- tanggal kosong → hari ini (WITA); tanggal di masa depan → hari ini
- foto yang bukan struk atau `confidence` rendah ditolak
- diskon/pajak level struk dibagi proporsional ke tiap item (`net_amount`),
  sehingga laporan per kategori selalu pas dengan total

## Kategori

`work`, `house`, `shopping`, `transportation`, `food`, `snack`,
`vegetable_fruit`, `health`, `entertainment`, `subscription`, `electronic`,
`donation`, `other`. Kategori disimpan **per item**, jadi satu struk
supermarket bisa terbagi ke beberapa kategori.

## Perintah

| Perintah | Fungsi |
|---|---|
| `/history` | Semua transaksi hari ini (`/history yesterday`, `/history 2026-11-01`) |
| `/today`, `/week`, `/month` | Total per kategori (`/month` + progres budget) |
| `/categories` | Daftar kategori |
| `/category <id> <kategori>` | Ubah kategori transaksi |
| `/delete <id>` | Hapus transaksi |
| `/budget` · `/budget <kategori> <nominal>` | Lihat / set budget bulanan (`2jt`, `500rb`; `0` = hapus) |
| `/export` · `/export 2026-10` | CSV transaksi satu bulan |
| `/logs` | 5 error terakhir |

## Setup lokal

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env      # lalu isi API_KEY, DATABASE_URL, TELEGRAM_*
python -m scripts.init_db   # buat tabel di Neon
pytest
uvicorn app.main:app --reload
```

## Deploy

1. Push ke GitHub, import di Vercel.
2. Isi environment variables Vercel sesuai `.env.example`, dengan
   `ENVIRONMENT=production`, `DEBUG=false`, `DOCS_ENABLED=false`.
3. Daftarkan webhook dan menu perintah:
   `python -m scripts.set_webhook https://<domain-vercel-kamu>`

## Struktur

```
app/
  api/routes/telegram.py    webhook: pesan, foto, callback tombol
  services/llm_client.py    prompt + JSON schema OpenAI
  services/expense_check.py validasi & pembagian diskon
  services/expense_flow.py  hasil analisis → draft / tersimpan / ringkasan
  services/callbacks.py     tombol inline
  services/commands.py      /history, /budget, /export, ...
  services/expense_log.py   query database
  db/models.py              receipts, receipt_items, budgets, processed_updates, error_logs
scripts/                    init_db, set_webhook
tests/                      pytest (SQLite in-memory + stub Telegram/OpenAI)
```
