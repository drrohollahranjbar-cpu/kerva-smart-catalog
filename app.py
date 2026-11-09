import os, sqlite3, secrets, json
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, abort, flash, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KERVA_DB", os.path.join(BASE, "kerva.db"))
UPLOAD_DIR = os.environ.get("KERVA_UPLOAD_DIR", os.path.join(BASE, "uploads"))
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("KERVA_SECRET", "CHANGE-THIS-TO-A-LONG-RANDOM-SECRET")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=8 * 1024 * 1024)
if os.environ.get("RAILWAY_ENVIRONMENT") and (not os.environ.get("KERVA_SECRET") or not os.environ.get("KERVA_ADMIN_PASSWORD") or not os.environ.get("KERVA_ADMIN_USER")):
    raise RuntimeError("Set KERVA_SECRET, KERVA_ADMIN_USER and KERVA_ADMIN_PASSWORD in Railway Variables before deployment.")
ALLOWED = {"png", "jpg", "jpeg", "webp"}

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, code TEXT DEFAULT '',
          description TEXT DEFAULT '', pack TEXT DEFAULT '', price INTEGER NOT NULL DEFAULT 0,
          stock INTEGER NOT NULL DEFAULT 0, discount REAL NOT NULL DEFAULT 0,
          image TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL,
          city TEXT NOT NULL, address TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(phone, city, address)
        );
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no TEXT UNIQUE NOT NULL, invoice_token TEXT NOT NULL,
          customer_id INTEGER NOT NULL, subtotal INTEGER NOT NULL, discount INTEGER NOT NULL,
          shipping INTEGER NOT NULL, total INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'ثبت‌شده',
          created_at TEXT NOT NULL, FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        CREATE TABLE IF NOT EXISTS order_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, product_id INTEGER,
          product_name TEXT NOT NULL, pack TEXT DEFAULT '', quantity INTEGER NOT NULL,
          unit_price INTEGER NOT NULL, line_total INTEGER NOT NULL,
          FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        );
        """)
        if not c.execute("SELECT id FROM admins LIMIT 1").fetchone():
            username = os.environ.get("KERVA_ADMIN_USER", "admin")
            password = os.environ.get("KERVA_ADMIN_PASSWORD", "Kerva-Change-2026!")
            c.execute("INSERT INTO admins(username,password_hash) VALUES(?,?)",
                      (username, generate_password_hash(password)))
init_db()

def admin_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapped

def toman(n):
    return f"{int(n):,}".replace(",", "٬")

@app.context_processor
def inject_helpers():
    return {"toman": toman}

@app.get("/health")
def health():
    return {"status": "ok"}, 200

@app.get("/")
def home():
    with db() as c:
        products = c.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    return render_template("shop.html", products=products, shipping_fee=max(0, int(os.environ.get("KERVA_SHIPPING_FEE", "0"))))

@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.get("/api/products")
def api_products():
    with db() as c:
        rows = c.execute("SELECT id,name,code,description,pack,price,stock,discount,image FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    return jsonify([dict(x) for x in rows])

@app.post("/api/checkout")
def checkout():
    data = request.get_json(silent=True) or {}
    customer = data.get("customer") or {}
    items = data.get("items") or []
    if not all(str(customer.get(k,"")).strip() for k in ("name","phone","city","address")):
        return jsonify(error="اطلاعات خریدار کامل نیست."), 400
    if not items:
        return jsonify(error="پیش‌فاکتور خالی است."), 400
    shipping = max(0, int(os.environ.get("KERVA_SHIPPING_FEE", "0")))
    # قیمت و تخفیف فقط از پایگاه داده خوانده می‌شود؛ ارقام ارسالی مرورگر معتبر نیست.
    with db() as c:
        clean, subtotal, discount_total = [], 0, 0
        for it in items:
            try:
                pid, qty = int(it["id"]), int(it["quantity"])
            except Exception:
                return jsonify(error="قلم نامعتبر است."), 400
            if qty < 1 or qty > 999:
                return jsonify(error="تعداد نامعتبر است."), 400
            p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not p:
                return jsonify(error="یکی از محصولات دیگر در دسترس نیست."), 409
            if qty > p["stock"]:
                return jsonify(error=f"موجودی {p['name']} کافی نیست."), 409
            unit = int(p["price"])
            discount_unit = int(unit * float(p["discount"]) / 100)
            final_unit = max(0, unit - discount_unit)
            line = final_unit * qty
            subtotal += unit * qty
            discount_total += discount_unit * qty
            clean.append((pid, p["name"], p["pack"], qty, final_unit, line))
        total = subtotal - discount_total + shipping
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        c.execute("INSERT OR IGNORE INTO customers(name,phone,city,address,created_at) VALUES(?,?,?,?,?)",
                  (customer["name"].strip(), customer["phone"].strip(), customer["city"].strip(), customer["address"].strip(), now))
        cust = c.execute("SELECT id FROM customers WHERE phone=? AND city=? AND address=?",
                         (customer["phone"].strip(), customer["city"].strip(), customer["address"].strip())).fetchone()
        invoice = "KRV-" + datetime.now().strftime("%y%m%d") + "-" + secrets.token_hex(3).upper()
        invoice_token = secrets.token_urlsafe(32)
        cur = c.execute("INSERT INTO orders(invoice_no,invoice_token,customer_id,subtotal,discount,shipping,total,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (invoice, invoice_token, cust["id"], subtotal, discount_total, shipping, total, now))
        oid = cur.lastrowid
        for pid, name, pack, qty, unit, line in clean:
            c.execute("INSERT INTO order_items(order_id,product_id,product_name,pack,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?,?)",
                      (oid, pid, name, pack, qty, unit, line))
            c.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, pid))
    return jsonify(ok=True, invoice=invoice, pdf=url_for("invoice_pdf", order_id=oid, token=invoice_token))

@app.get("/invoice/<int:order_id>/<token>.pdf")
def invoice_pdf(order_id, token):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from io import BytesIO
    except ImportError:
        return "برای تولید PDF کتابخانه reportlab را نصب کنید.", 500
    with db() as c:
        order = c.execute("""SELECT o.*,c.name,c.phone,c.city,c.address FROM orders o
                            JOIN customers c ON c.id=o.customer_id WHERE o.id=?""",(order_id,)).fetchone()
        if not order or (not session.get("admin_id") and not secrets.compare_digest(order["invoice_token"], token)):
            abort(404)
        items = c.execute("SELECT * FROM order_items WHERE order_id=?",(order_id,)).fetchall()
    # برای اتصال حروف و راست‌به‌چپ شدن متن فارسی از arabic-reshaper و python-bidi استفاده می‌شود.
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    w,h = A4
    font_path = os.path.join(BASE,"fonts","Vazirmatn-Regular.ttf")
    if os.path.exists(font_path):
        pdfmetrics.registerFont(TTFont("Vazir",font_path)); font="Vazir"
    else: font="Helvetica"
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        shape = lambda x: get_display(arabic_reshaper.reshape(str(x)))
    except Exception:
        shape = lambda x: str(x)
    pdf.setFont(font, 16); pdf.setFillColor(colors.HexColor("#174b35"))
    pdf.drawRightString(w-22*mm,h-22*mm,shape("Kerva | پیش‌فاکتور"))
    pdf.setFillColor(colors.black); pdf.setFont(font, 9)
    y=h-38*mm
    for line in [f"شماره: {order['invoice_no']}", f"تاریخ: {order['created_at'][:10]}",
                 f"خریدار: {order['name']}    تماس: {order['phone']}",
                 f"شهر: {order['city']}", f"نشانی: {order['address']}"]:
        pdf.drawRightString(w-22*mm,y,shape(line)); y-=7*mm
    y-=5*mm
    pdf.setFillColor(colors.HexColor("#e9f1eb")); pdf.rect(20*mm,y-5*mm,w-40*mm,9*mm,fill=1,stroke=0)
    pdf.setFillColor(colors.black); pdf.drawRightString(188*mm,y-2*mm,shape("مبلغ (تومان)"))
    pdf.drawRightString(145*mm,y-2*mm,shape("تعداد"))
    pdf.drawRightString(112*mm,y-2*mm,shape("بسته‌بندی"))
    pdf.drawRightString(78*mm,y-2*mm,shape("محصول"))
    y-=13*mm
    for it in items:
        pdf.drawRightString(78*mm,y,shape(it["product_name"][:34]))
        pdf.drawRightString(112*mm,y,shape(it["pack"][:18]))
        pdf.drawRightString(145*mm,y,str(it["quantity"]))
        pdf.drawRightString(188*mm,y,toman(it["line_total"]))
        y-=8*mm
        if y<45*mm:
            pdf.showPage(); y=h-25*mm; pdf.setFont(font,9)
    y-=5*mm
    for label,val in [("جمع قبل از تخفیف",order["subtotal"]),("تخفیف",order["discount"]),
                      ("هزینه ارسال",order["shipping"]),("مبلغ نهایی",order["total"])]:
        pdf.drawRightString(188*mm,y,shape(f"{label}: {toman(val)} تومان")); y-=8*mm
    pdf.setFont(font,8); pdf.drawRightString(w-22*mm,18*mm,shape("این سند پیش‌فاکتور است و پس از تأیید فروشنده نهایی می‌شود."))
    pdf.save(); buf.seek(0)
    response=make_response(buf.getvalue())
    response.headers["Content-Type"]="application/pdf"
    response.headers["Content-Disposition"]=f'attachment; filename="{order["invoice_no"]}.pdf"'
    return response

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method=="POST":
        with db() as c:
            row=c.execute("SELECT * FROM admins WHERE username=?",(request.form.get("username",""),)).fetchone()
        if row and check_password_hash(row["password_hash"],request.form.get("password","")):
            session.clear(); session["admin_id"]=row["id"]
            return redirect(url_for("admin_dashboard"))
        flash("نام کاربری یا رمز عبور اشتباه است.")
    return render_template("login.html")

@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.get("/admin")
@admin_required
def admin_dashboard():
    with db() as c:
        products=c.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        orders=c.execute("""SELECT o.*,c.name,c.phone,c.city FROM orders o JOIN customers c ON c.id=o.customer_id ORDER BY o.id DESC""").fetchall()
        customers=c.execute("SELECT * FROM customers ORDER BY id DESC").fetchall()
    return render_template("admin.html",products=products,orders=orders,customers=customers)

@app.post("/admin/product")
@admin_required
def save_product():
    f=request.files.get("image")
    image=""
    if f and f.filename:
        ext=f.filename.rsplit(".",1)[-1].lower()
        if ext not in ALLOWED: flash("فرمت تصویر مجاز نیست."); return redirect(url_for("admin_dashboard"))
        image=secrets.token_hex(12)+"."+ext
        f.save(os.path.join(UPLOAD_DIR,image))
    data=[request.form.get("name","").strip(),request.form.get("code","").strip(),
          request.form.get("description","").strip(),request.form.get("pack","").strip(),
          max(0,int(request.form.get("price") or 0)),max(0,int(request.form.get("stock") or 0)),
          min(100,max(0,float(request.form.get("discount") or 0)))]
    if not data[0]: flash("نام محصول الزامی است."); return redirect(url_for("admin_dashboard"))
    pid=request.form.get("id")
    with db() as c:
        if pid:
            if image:
                c.execute("""UPDATE products SET name=?,code=?,description=?,pack=?,price=?,stock=?,discount=?,image=?,active=? WHERE id=?""",
                          (*data,image,1 if request.form.get("active")=="on" else 0,pid))
            else:
                c.execute("""UPDATE products SET name=?,code=?,description=?,pack=?,price=?,stock=?,discount=?,active=? WHERE id=?""",
                          (*data,1 if request.form.get("active")=="on" else 0,pid))
        else:
            c.execute("""INSERT INTO products(name,code,description,pack,price,stock,discount,image,active,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (*data,image,1 if request.form.get("active")=="on" else 0,datetime.now().isoformat(timespec="seconds")))
    flash("محصول ذخیره شد.")
    return redirect(url_for("admin_dashboard"))

@app.post("/admin/product/<int:pid>/delete")
@admin_required
def delete_product(pid):
    with db() as c:
        c.execute("DELETE FROM products WHERE id=?",(pid,))
    flash("محصول حذف شد.")
    return redirect(url_for("admin_dashboard"))

@app.post("/admin/order/<int:oid>/status")
@admin_required
def order_status(oid):
    status=request.form.get("status","ثبت‌شده")
    if status not in ("ثبت‌شده","درحال‌بررسی","تأییدشده","ارسال‌شده","لغوشده"): status="ثبت‌شده"
    with db() as c: c.execute("UPDATE orders SET status=? WHERE id=?",(status,oid))
    return redirect(url_for("admin_dashboard"))

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=False)
