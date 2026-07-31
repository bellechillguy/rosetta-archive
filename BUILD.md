# Instruksi build

RTA ditulis dengan Python standard library dan tidak memiliki tahap kompilasi native. Build minimum memeriksa seluruh source dan menyiapkan launcher lokal.

## Build tanpa dependency

Jalankan dari root repository:

```bash
chmod +x rta scripts/demo.sh
python3 -m compileall -q src
./rta --version
```

Python 3.10 atau lebih baru diperlukan. Command utama dapat dijalankan melalui launcher lokal:

```bash
./rta create contoh.rta folder/
./rta verify contoh.rta
./rta extract contoh.rta hasil/
```

Launcher `./rta` menambahkan direktori `src` ke Python path, sehingga aplikasi dapat dijalankan tanpa `pip` atau `setuptools`.

## Pengujian

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Instalasi entry point, opsional

Repository menyertakan `pyproject.toml`. Jika environment memiliki `setuptools`, entry point `rta` dapat dipasang dalam virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools
python -m pip install -e .
rta --version
```

Bagian ini opsional. Jalur utama tetap menggunakan `./rta` dan tidak membutuhkan dependency runtime di luar standard library.

## Demonstrasi otomatis

```bash
./scripts/demo.sh
```

Skrip akan:

1. membuat data teks, biner, dan direktori kosong
2. menjalankan `create`, `list`, `info`, `inspect`, `verify`, dan `extract`
3. membandingkan input dengan hasil ekstraksi
4. membuktikan output deterministik
5. merusak satu payload dan menunjukkan penolakan
6. menjalankan recovery parsial
7. menjalankan seluruh test suite

Output sementara dibuat di `demo-work` dan diabaikan oleh Git.

## Verifikasi sebelum rilis

```bash
python3 -m compileall -q src
PYTHONPATH=src python3 -m unittest discover -s tests -v
./scripts/demo.sh
```

Semua test harus berakhir dengan status `OK`.
