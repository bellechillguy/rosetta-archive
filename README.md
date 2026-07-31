# Rosetta Archive

Rosetta Archive, disingkat RTA, adalah format arsip biner orisinal dengan payload di depan dan indeks di bagian akhir. Implementasi referensi ini menyimpan beberapa file dan direktori, metadata POSIX, informasi versioning, CRC32, SHA-256, dan data yang dapat dikompresi per file.

Format dan implementasi saat ini berada pada versi 1.0. Seluruh kode runtime menggunakan Python standard library.

## Fitur

- enam command utama: `create`, `list`, `info`, `inspect`, `verify`, dan `extract`
- traversal direktori secara rekursif, termasuk direktori kosong
- penyimpanan file biner tanpa perubahan isi
- metadata mode, uid, gid, dan mtime nanosecond
- CRC32 untuk struktur, payload tersimpan, dan isi asli
- SHA-256 untuk indeks dan isi asli
- kompresi zlib per file jika hasilnya lebih kecil
- indeks akhir dengan offset absolut untuk akses berbasis seek
- output deterministik untuk input, metadata, opsi, dan versi zlib yang sama
- ekstraksi yang menolak path traversal, symlink output, dan overwrite
- command bonus `recover` untuk menyelamatkan entri yang masih valid

## Organisasi format

```text
+---------------------------+
| Header, 112 byte          |
+---------------------------+
| Payload file              |
| ...                       |
+---------------------------+
| Index header              |
| Entry records             |
+---------------------------+
| Recovery footer, 64 byte  |
+---------------------------+
```

Header menyimpan identitas format, versi, ukuran arsip, lokasi indeks, flags, CRC32, dan SHA-256 indeks. Setiap entry record menyimpan path canonical, tipe, metadata, ukuran, metode penyimpanan, offset payload, CRC32, dan SHA-256. Recovery footer mengulang lokasi indeks agar proses pemulihan dapat dimulai dari akhir arsip.

## Kebutuhan

- Python 3.10 atau lebih baru
- tidak ada dependency runtime di luar standard library

## Menjalankan tanpa instalasi

```bash
chmod +x rta
./rta --help
./rta --version
```

Membuat dan membaca arsip:

```bash
./rta create contoh.rta folder/ file.bin
./rta list contoh.rta
./rta info contoh.rta
./rta inspect contoh.rta
./rta verify contoh.rta
./rta extract contoh.rta hasil/
```

Memulihkan entri yang masih valid dari arsip rusak:

```bash
./rta recover arsip-rusak.rta arsip-pulih.rta
./rta verify arsip-pulih.rta
```

Lihat [BUILD.md](BUILD.md) untuk instruksi build, pengujian, instalasi opsional, dan demonstrasi otomatis.

## Perbedaan command inspeksi

- `list` menampilkan tipe, ukuran, metode, dan path setiap entri.
- `info` menampilkan ringkasan arsip seperti versi, ukuran, jumlah entri, dan lokasi indeks.
- `inspect` menampilkan field header, metadata, offset, CRC32, dan SHA-256.
- `verify` membaca struktur dan seluruh payload tanpa mengekstrak file.
- `extract` menjalankan verifikasi penuh sebelum membuat output.

## Pengujian

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Test suite mencakup round trip file teks dan biner, direktori kosong, metadata, determinisme, perubahan payload, arsip truncated, versi yang tidak didukung, path traversal dengan checksum struktur yang valid, entri duplikat, offset di luar batas, recovery parsial, dan seluruh command wajib.

Demonstrasi otomatis:

```bash
./scripts/demo.sh
```

Skrip membuat data sementara di `demo-work`, menjalankan command utama, membandingkan hasil ekstraksi, merusak satu payload, mencoba recovery, dan menjalankan test suite. Direktori tersebut diabaikan oleh Git.

## Bonus yang diimplementasikan

- Recovery parsial: `recover` melewati record atau payload yang rusak dan menghasilkan arsip baru dari entri yang masih valid.
- Kompresi per file: writer memilih zlib hanya ketika hasilnya lebih kecil dan menyediakan opsi `--store` serta `--compression-level`.
- Random access pada reader: record indeks menyimpan offset absolut sehingga reader dapat melakukan seek langsung ke payload.
- Streaming pada reader: verifikasi dan ekstraksi membaca payload dalam chunk 1 MiB.

Streaming belum penuh pada sisi writer. `create` dan `recover` masih menahan isi file di memori. Format juga memiliki version field dan extension TLV, tetapi repository ini belum menyertakan implementasi versi kedua untuk membuktikan kompatibilitas nyata antarversi.

## Keamanan dan batas implementasi

Parser memeriksa magic, version compatibility, flags, reserved fields, batas indeks, jumlah record, ukuran, offset, gap, overlap, duplikat, parent directory, path canonical, dan checksum sebelum data diproses lebih lanjut. Extractor memverifikasi seluruh arsip sebelum menulis output.

Versi 1.0 hanya mendukung file biasa dan direktori. Symlink dan special file ditolak. Format belum mendukung ACL, extended attribute, hard link, sparse file, enkripsi, atau tanda tangan digital.

## Dokumen pengumpulan

- [docs/pdf/RTA_Format_Specification.pdf](docs/pdf/RTA_Format_Specification.pdf)
- [docs/pdf/RTA_Report.pdf](docs/pdf/RTA_Report.pdf)

Video demonstrasi: [YouTube](https://youtu.be/RBysrufzUBo)


## Lisensi

Kode sumber tersedia di bawah [MIT License](LICENSE).
