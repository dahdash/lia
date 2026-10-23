"""Demo site for Lia: a tiny shop page. v1 (healthy)."""

import os

from flask import Flask, jsonify

app = Flask(__name__)
VERSION = "1.0"
PRODUCTS = [("قهوه", 180_000), ("چای", 95_000), ("کیک", 120_000)]

# Controlled failure for the "slow site / out of memory" scenario:
# MEMORY_HOG_MB=600 makes the app hold that much RAM (more than small-g2's 512 MB).
_hog = bytearray(int(os.environ.get("MEMORY_HOG_MB", "0")) * 1024 * 1024)


@app.route("/")
def index():
    items = "".join(f"<li>{n} — {p:,} تومان</li>" for n, p in PRODUCTS)
    return (f"<html dir='rtl'><meta charset='utf-8'><title>فروشگاه</title>"
            f"<h1>فروشگاه نمونه</h1><ul>{items}</ul><small>نسخه {VERSION}</small></html>")


@app.route("/health")
def health():
    return jsonify(status="ok", version=VERSION)
