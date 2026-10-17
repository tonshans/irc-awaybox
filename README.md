# IRC AwayBox

> **Small persistent IRC mailbox with Telegram remote control, auto-replies, and plain-text logging.**

AwayBox keeps your presence on IRC while you are offline. It archives messages to transparent plain-text log files, dispatches push notifications to your private Telegram account with 1-click reply buttons, auto-responds to peers when you're away, and lets you manage your IRC channels directly from Telegram.

---

## Fitur 

- **Zero Inbound Ports**: Bekerja sepenuhnya melalui koneksi keluar (TLS IRC + Telegram long polling). Tidak membutuhkan IP publik, port forwarding, atau webhook.
- **Plain-Text Files**: Semua riwayat disimpan rapi di `/var/lib/awaybox/logs/` dalam format teks biasa UTF-8 (`channels/` dan `private/`).
- **Telegram Remote Control**:
  - Notifikasi pesan instan dengan tombol inline `Reply Channel` dan `Private <sender>`.
  - Satu ketukan untuk mengaktifkan sesi balasan berbatas waktu.
  - Perintah kendali lengkap: `/status`, `/channels`, `/msg`, `/join`, `/leave`, `/autoreply`, `/ignore`, `/unignore`, `/log`, `/cancel`.
- **Auto-Reply & Anti-Spam Cooldown**:
  - Respons otomatis terukur untuk private message dan channel tertentu.
  - Cooldown cerdas per-nickname (case-insensitive) bersama agar tidak membanjiri lawan bicara.
- **Proteksi Total Ignore List**:
  - Nickname yang diabaikan tidak akan pernah dicatat ke log, tidak dikirim ke Telegram, dan tidak memicu balasan.
- **Keamanan & Non-Root**:
  - Berjalan di bawah akun sistem khusus `awaybox`.
  - Hardening ketat melalui systemd (`ProtectSystem=strict`, `NoNewPrivileges=true`).
  - Pemisahan data sensitif di `/etc/awaybox/secrets.env` (chmod 600).
- **Tahan Banting & Isolasi Gangguan**:
  - Gangguan koneksi Telegram tidak pernah mematikan perekaman log IRC.
  - Gangguan koneksi IRC tidak mematikan kendali bot Telegram.
  - Reconnect otomatis dengan backoff eksponensial (5s, 10s, 20s, 40s, 80s, 160s, max 300s).

---

## Struktur Direktori

```text
/opt/awaybox/                # Direktori instalasi aplikasi & venv
/etc/awaybox/
├── config.toml              # Konfigurasi permanen
└── secrets.env              # Kredensial rahasia (chmod 600)
/var/lib/awaybox/logs/
├── recorder.log             # Log aktivitas layanan dan error
├── channels/                # Log per-channel (contoh: indonesia.log)
└── private/                 # Log per-pengirim (contoh: Alice.log)
```

---

## Petunjuk Installasi

### 1. Kloning Repositori
```bash
git clone https://github.com/tonshans/irc-awaybox.git
cd awaybox
```

### 2. Jalankan Installer
Script `setup.sh` bersifat **idempoten** dan aman dijalankan berulang kali:
```bash
sudo ./setup.sh
```

### 3. Masukkan Kredensial & Token
Edit file rahasia:
```bash
sudo nano /etc/awaybox/secrets.env
```
Isi dengan data Anda:
```env
IRC_PASSWORD="password_nickserv_anda"
IRC_CHAN_PASSWORD="password_channel_anda"
TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIjkLmNoPqRsTuVwXyZ"
TELEGRAM_ALLOWED_USER_ID=123456789
```

### 4. Sesuaikan Konfigurasi Server & Channel
```bash
sudo nano /etc/awaybox/config.toml
```

### 5. Jalankan Layanan
```bash
sudo systemctl start awaybox
sudo systemctl status awaybox
```

---

## Daftar Perintah Telegram

| Perintah | Deskripsi |
| :--- | :--- |
| `/start` | Menampilkan panduan dan daftar perintah |
| `/status` | Status koneksi IRC, Telegram, channel aktif, uptime |
| `/channels` | Menampilkan channel IRC yang sedang aktif |
| `/msg #ch/Nick <pesan>` | Mengirim pesan langsung dari Telegram ke channel/nickname IRC |
| `/join #channel` | Meminta AwayBox bergabung ke channel |
| `/leave #channel` | Meminta AwayBox meninggalkan channel |
| `/autoreply` | Menampilkan pengaturan auto-reply saat ini |
| `/autoreply on` | Mengaktifkan auto-reply secara dinamis |
| `/autoreply off` | Menonaktifkan auto-reply secara dinamis |
| `/autoreply add #ch` | Menambahkan channel ke daftar auto-reply |
| `/autoreply remove #ch`| Menghapus channel dari daftar auto-reply |
| `/ignore [Nick]` | Menampilkan atau menambahkan nickname ke daftar abaikan |
| `/unignore Nick` | Menghapus nickname dari daftar abaikan |
| `/log #channel` | Mengambil 20 baris riwayat terakhir channel |
| `/log Nick` | Mengambil 20 baris riwayat terakhir pesan pribadi |
| `/reconnect [now\|auto\|stop]` | Kontrol reconnect IRC dari Telegram |
| `/conirc` | Menghubungkan (atau menyambung ulang) koneksi bot ke IRC sekarang |
| `/disconirc` | Memutus koneksi bot dari IRC; tetap offline sampai `/conirc` |
| `/cancel` | Membatalkan sesi balasan yang sedang aktif |

---
