"""Demo site for Lia: v2 adds a discount banner — and an intentional bug.

The banner reads DISCOUNT_PERCENT from the environment without a default, so if
the variable is missing every request to / fails with KeyError -> HTTP 500.
"""

import os

from flask import Flask, jsonify

app = Flask(__name__)
VERSION = "2.0"
PRODUCTS = [("قهوه", 180_000), ("چای", 95_000), ("کیک", 120_000)]

_hog = bytearray(int(os.environ.get("MEMORY_HOG_MB", "0")) * 1024 * 1024)


@app.route("/")
def index():
    discount = int(os.environ["DISCOUNT_PERCENT"])  # bug: no default
    items = "".join(
        f"<li>{n} — <s>{p:,}</s> {p * (100 - discount) // 100:,} تومان</li>" for n, p in PRODUCTS)
    return (f"<html dir='rtl'><meta charset='utf-8'><title>فروشگاه</title>"
            f"<h1>فروشگاه نمونه — {discount}٪ تخفیف!</h1><ul>{items}</ul>"
            f"<small>نسخه {VERSION}</small></html>")


@app.route("/health")
def health():
    return jsonify(status="ok", version=VERSION)
