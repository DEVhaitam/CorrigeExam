#!/usr/bin/env python3
"""
generate_pdfs.py — create synthetic exam PDFs for load testing.

Generates three sizes matching the W2 upload workload distribution:
  exam-200k.pdf   ~200 KB  (70% of uploads)
  exam-2m.pdf     ~2 MB    (25% of uploads)
  exam-10m.pdf    ~10 MB   ( 5% of uploads)

PDFs are written to workload/data/synthetic-exams/ and are reused across
runs. Run once before the first experiment; re-run only if sizes change.

Usage:
  python workload/data/generate_pdfs.py
  python workload/data/generate_pdfs.py --force   # overwrite existing
"""

import argparse
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "synthetic-exams"

TARGETS = [
    ("exam-200k.pdf",  200 * 1024),
    ("exam-2m.pdf",    2   * 1024 * 1024),
    ("exam-10m.pdf",   10  * 1024 * 1024),
]


def _make_pdf(target_bytes: int) -> bytes:
    """
    Build a minimal valid PDF padded to approximately target_bytes.

    Structure:
      - PDF header + one page with a text object
      - A single large stream object filled with compressed zeros
        (compresses well, ensuring the on-disk size is controlled via
        the *uncompressed* size we choose)
      - Cross-reference table + trailer

    The padding stream is just zlib-compressed zeros; the backend will
    accept it as a valid PDF (the content is irrelevant for load testing).
    """
    # We'll inflate the stream until the total file is near target_bytes.
    # One pass: estimate how much uncompressed content is needed.
    # zlib level-9 compresses all-zeros to ~0.1% of original, so we need
    # a large uncompressed blob to produce a decently-sized file.
    # Strategy: produce uncompressed content proportional to target size,
    # then compress and check. For simplicity just fill with varied bytes.

    # Build the PDF objects
    objects = []

    # Object 1: Catalog
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")

    # Object 2: Pages dict
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")

    # Object 3: Page
    objects.append(
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R\n"
        b"   /MediaBox [0 0 612 792]\n"
        b"   /Contents 4 0 R\n"
        b"   /Resources << /Font << /F1 5 0 R >> >> >>\n"
        b"endobj\n"
    )

    # Object 4: Page content stream (text)
    content = b"BT /F1 12 Tf 72 720 Td (CorrectExam synthetic exam) Tj ET"
    objects.append(
        b"4 0 obj\n"
        + f"<< /Length {len(content)} >>\n".encode()
        + b"stream\n"
        + content
        + b"\nendstream\nendobj\n"
    )

    # Object 5: Font
    objects.append(
        b"5 0 obj\n"
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n"
        b"endobj\n"
    )

    # Object 6: Uncompressed padding stream — file size on disk equals target_bytes.
    # The upload path (ScanService.uploadFile) streams bytes to MinIO without
    # PDFBox parsing, so JVM heap stress scales with the raw multipart payload size.
    # Using uncompressed stream so the on-disk PDF size matches target_bytes exactly.
    header_overhead = sum(len(o) for o in objects) + 500  # rough xref+trailer estimate
    padding_size = max(0, target_bytes - header_overhead)
    # Repeating 0xFF bytes: still a valid uncompressed PDF stream
    raw_padding = b"\xff" * padding_size
    objects.append(
        b"6 0 obj\n"
        + f"<< /Length {len(raw_padding)} >>\n".encode()
        + b"stream\n"
        + raw_padding
        + b"\nendstream\nendobj\n"
    )

    # Assemble with cross-reference table
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    body = b"".join(objects)

    # Build xref
    offsets = []
    pos = len(header)
    for obj in objects:
        offsets.append(pos)
        pos += len(obj)

    xref_offset = pos
    xref = b"xref\n"
    xref += f"0 {len(objects) + 1}\n".encode()
    xref += b"0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()

    trailer = (
        b"trailer\n"
        + f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
        + b"startxref\n"
        + f"{xref_offset}\n".encode()
        + b"%%EOF\n"
    )

    return header + body + xref + trailer


def generate(force: bool = False) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, target in TARGETS:
        path = OUTPUT_DIR / filename
        if path.exists() and not force:
            print(f"  skip  {filename}  ({path.stat().st_size / 1024:.0f} KB, already exists)")
            continue
        pdf = _make_pdf(target)
        path.write_bytes(pdf)
        print(f"  wrote {filename}  ({len(pdf) / 1024:.0f} KB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic exam PDFs.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()
    print(f"Writing synthetic PDFs to {OUTPUT_DIR}")
    generate(force=args.force)
    print("Done.")
