# Rosetta Archive

Rosetta Archive, atau RTA, adalah format arsip biner buatan sendiri. Format ini menempatkan payload di bagian awal arsip dan indeks di bagian akhir.

Implementasi referensinya dapat menyimpan beberapa file dan direktori, mempertahankan metadata POSIX, memeriksa integritas dengan CRC32 dan SHA-256, serta mengompresi setiap file jika hasil kompresinya lebih kecil.

Format dan implementasi saat ini menggunakan versi 1.0. Seluruh kode runtime hanya memakai pustaka standar Python.

## Fitur

- Enam command utama: `create`, `list`, `info`, `inspect`, `verify`, dan `extract`.
- Traversal direktori secara rekursif, termasuk direktori kosong.
- Penyimpanan file biner tanpa mengubah isinya.
- Metadata POSIX berupa mode, UID, GID, dan `mtime` dalam nanodetik.
- CRC32 untuk struktur arsip, payload tersimpan, dan isi asli.
- SHA-256 untuk indeks dan isi asli.
- Kompresi zlib per file jika ukuran hasil kompresi lebih kecil.
- Indeks di bagian akhir arsip dengan offset absolut untuk akses langsung ke payload.
- Output deterministik jika input, metadata, opsi, dan versi zlib sama.
- Ekstraksi yang menolak path traversal, symlink pada lokasi output, dan penimpaan file.
- Command tambahan `recover` untuk menyelamatkan entri yang masih valid dari arsip rusak.

## Susunan format

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

Header menyimpan magic format, versi, ukuran arsip, lokasi indeks, flags, CRC32, dan SHA-256 indeks.

Setiap entry record berisi jalur kanonis, tipe entri, metadata, ukuran, metode penyimpanan, offset payload, CRC32, dan SHA-256.

Recovery footer menyimpan kembali lokasi indeks. Informasi ini memungkinkan proses pemulihan dimulai dari bagian akhir arsip.

## Kebutuhan

- Python 3.10 atau versi yang lebih baru.
- Tidak ada dependency runtime di luar pustaka standar Python.

## Menjalankan tanpa instalasi

Berikan izin eksekusi pada launcher, lalu periksa bantuan dan versi program:

```bash
chmod +x rta
./rta --help
./rta --version
```

### Membuat dan membaca arsip

```bash
./rta create contoh.rta folder/ file.bin
./rta list contoh.rta
./rta info contoh.rta
./rta inspect contoh.rta
./rta verify contoh.rta
./rta extract contoh.rta hasil/
```

### Memulihkan arsip rusak

Command `recover` membaca entri satu per satu dan membuat arsip baru dari entri yang masih dapat diverifikasi.

```bash
./rta recover arsip-rusak.rta arsip-pulih.rta
./rta verify arsip-pulih.rta
```

Instruksi build, pengujian, instalasi opsional, dan demonstrasi otomatis tersedia di [BUILD.md](BUILD.md).

## Perbedaan command

- `list` menampilkan tipe, ukuran, metode penyimpanan, dan jalur setiap entri.
- `info` menampilkan ringkasan arsip, termasuk versi, ukuran, jumlah entri, dan lokasi indeks.
- `inspect` menampilkan field header, metadata entri, offset, CRC32, dan SHA-256.
- `verify` memeriksa struktur arsip dan seluruh payload tanpa mengekstrak file.
- `extract` menjalankan verifikasi penuh sebelum membuat file atau direktori output.

## Pengujian

Jalankan seluruh test suite dengan perintah berikut:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Pengujian mencakup:

- round trip untuk file teks dan biner;
- direktori kosong;
- metadata;
- output deterministik;
- perubahan payload;
- arsip yang terpotong;
- versi format yang tidak didukung;
- path traversal dengan checksum struktur yang valid;
- entri duplikat;
- offset di luar batas;
- recovery parsial; dan
- seluruh command wajib.

### Demonstrasi otomatis

```bash
./scripts/demo.sh
```

Script membuat data sementara di `demo-work`, menjalankan command utama, dan membandingkan hasil ekstraksi dengan data awal. Setelah itu, script merusak satu payload, mencoba proses recovery, lalu menjalankan seluruh test suite.

Direktori `demo-work` diabaikan oleh Git.

## Fitur tambahan

### Recovery parsial

Command `recover` melewati record atau payload yang rusak. Entri yang masih valid ditulis ke arsip baru.

### Kompresi per file

Writer menggunakan zlib hanya jika hasil kompresinya lebih kecil daripada data asli. Pengguna juga dapat memilih mode penyimpanan tanpa kompresi melalui `--store` atau mengatur level kompresi melalui `--compression-level`.

### Akses langsung pada reader

Record indeks menyimpan offset absolut. Reader dapat melakukan `seek` langsung ke payload tanpa membaca seluruh arsip dari awal.

### Pembacaan secara streaming

Proses verifikasi dan ekstraksi membaca payload dalam potongan berukuran 1 MiB.

Writer belum sepenuhnya menggunakan streaming. Command `create` dan `recover` masih memuat isi file ke memori sebelum menulisnya ke arsip.

Format sudah memiliki field versi dan extension TLV. Repositori ini belum menyertakan implementasi format versi kedua, sehingga kompatibilitas antarversi belum diuji pada dua implementasi format yang berbeda.

## Keamanan dan batas implementasi

Sebelum memproses payload, parser memeriksa:

- magic format;
- kompatibilitas versi;
- flags dan reserved fields;
- batas indeks;
- jumlah record;
- ukuran dan offset;
- gap dan overlap;
- entri duplikat;
- parent directory;
- jalur kanonis; dan
- checksum.

Extractor memverifikasi seluruh arsip sebelum menulis output.

Versi 1.0 hanya mendukung file biasa dan direktori. Symlink dan special file ditolak.

Format ini belum mendukung:

- ACL;
- extended attribute;
- hard link;
- sparse file;
- enkripsi; dan
- tanda tangan digital.

## Dokumen pengumpulan

- [Spesifikasi format RTA](docs/pdf/RTA_Format_Specification.pdf)
- [Laporan RTA](docs/pdf/RTA_Report.pdf)
- [Video demonstrasi](https://youtu.be/RBysrufzUBo)

## Lisensi

Kode sumber tersedia di bawah [MIT License](LICENSE).
