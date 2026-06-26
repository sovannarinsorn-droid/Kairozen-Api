from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import secrets, string, requests, datetime, os, threading, time, uuid

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kairozen-secret-2025")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///kairozen.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ─── Models ───────────────────────────────────────────────────────────────────

class User(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(120), unique=True, nullable=False)
    password   = db.Column(db.String(256), nullable=False)
    bakong_id  = db.Column(db.String(100), nullable=True)
    shop_name  = db.Column(db.String(100), nullable=True)
    api_key    = db.Column(db.String(64), unique=True, nullable=True)
    total_paid = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

class Payment(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    pay_id         = db.Column(db.String(64), unique=True, default=lambda: str(uuid.uuid4())[:16])
    user_id        = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    md5            = db.Column(db.String(128), nullable=True)
    transaction_id = db.Column(db.String(128), nullable=True)
    amount         = db.Column(db.Float, nullable=False)
    currency       = db.Column(db.String(10), default="KHR")
    note           = db.Column(db.String(200), default="")
    status         = db.Column(db.String(20), default="PENDING")   # PENDING / PAID / EXPIRED
    qr_string      = db.Column(db.Text, nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    paid_at        = db.Column(db.DateTime, nullable=True)
    expire_minutes = db.Column(db.Integer, default=10)
    webhook_url    = db.Column(db.String(300), nullable=True)       # optional callback

# ─── Helpers ──────────────────────────────────────────────────────────────────

def gen_api_key():
    chars = string.ascii_letters + string.digits
    return "kz_" + "".join(secrets.choice(chars) for _ in range(40))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def api_key_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("x-api-key") or request.args.get("api_key")
        if not key:
            return jsonify({"success": False, "error": "Missing API key"}), 401
        user = User.query.filter_by(api_key=key).first()
        if not user:
            return jsonify({"success": False, "error": "Invalid API key"}), 401
        if not user.bakong_id or not user.shop_name:
            return jsonify({"success": False, "error": "Bakong ID or Shop Name not set"}), 400
        request.api_user = user
        return f(*args, **kwargs)
    return decorated

CAMRAPIDPAY_CREATE = "https://camrapidpay.com/api/v1/khqr/create-payments"
CAMRAPIDPAY_CHECK  = "https://camrapidpay.com/api/v1/khqr/check-transaction-api"

# ─── Auto-Polling Engine ──────────────────────────────────────────────────────

_poll_lock    = threading.Lock()
_active_polls = {}   # pay_id -> threading.Event (set = stop)

def _do_poll(pay_id, interval=5):
    """Background thread: poll CamRapidPay every `interval` seconds until paid/expired."""
    stop_event = _active_polls.get(pay_id)
    while stop_event and not stop_event.is_set():
        try:
            with app.app_context():
                pmt = Payment.query.filter_by(pay_id=pay_id).first()
                if not pmt or pmt.status != "PENDING":
                    break

                # Check expiry
                age = (datetime.datetime.utcnow() - pmt.created_at).total_seconds() / 60
                if age >= pmt.expire_minutes:
                    pmt.status = "EXPIRED"
                    db.session.commit()
                    break

                # Build check params
                params = {}
                if pmt.md5:
                    params["md5"] = pmt.md5
                elif pmt.transaction_id:
                    params["transactionId"] = pmt.transaction_id
                else:
                    break

                r = requests.get(CAMRAPIDPAY_CHECK, params=params, timeout=10)
                result = r.json()

                paid = (result.get("status") == "SUCCESS" or
                        result.get("isPaid") is True or
                        result.get("data", {}).get("status") == "SUCCESS")

                if paid:
                    pmt.status  = "PAID"
                    pmt.paid_at = datetime.datetime.utcnow()
                    user = User.query.get(pmt.user_id)
                    if user:
                        user.total_paid += pmt.amount
                    db.session.commit()

                    # Fire webhook if set
                    if pmt.webhook_url:
                        try:
                            requests.post(pmt.webhook_url, json={
                                "pay_id":   pmt.pay_id,
                                "status":   "PAID",
                                "amount":   pmt.amount,
                                "currency": pmt.currency,
                                "note":     pmt.note,
                                "paid_at":  pmt.paid_at.isoformat(),
                            }, timeout=8)
                        except:
                            pass
                    break

        except Exception as e:
            pass  # keep polling on transient errors

        time.sleep(interval)

    with _poll_lock:
        _active_polls.pop(pay_id, None)

def start_poll(pay_id, interval=5):
    with _poll_lock:
        if pay_id in _active_polls:
            return
        ev = threading.Event()
        _active_polls[pay_id] = ev
    t = threading.Thread(target=_do_poll, args=(pay_id, interval), daemon=True)
    t.start()

def stop_poll(pay_id):
    ev = _active_polls.get(pay_id)
    if ev:
        ev.set()

# ─── Pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        data = request.get_json() or request.form
        email    = data.get("email", "").strip().lower()
        password = data.get("password", "")
        if not email or not password:
            return jsonify({"success": False, "error": "Email និង Password ត្រូវការ"}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({"success": False, "error": "Email នេះបានចុះឈ្មោះហើយ"}), 409
        user = User(email=email, password=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        session["user_id"] = user.id
        return jsonify({"success": True, "redirect": "/dashboard"})
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json() or request.form
        email    = data.get("email", "").strip().lower()
        password = data.get("password", "")
        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password, password):
            return jsonify({"success": False, "error": "Email ឬ Password មិនត្រឹមត្រូវ"}), 401
        session["user_id"] = user.id
        return jsonify({"success": True, "redirect": "/dashboard"})
    return render_template("auth.html", mode="login")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = User.query.get(session["user_id"])
    payments = Payment.query.filter_by(user_id=user.id).order_by(Payment.created_at.desc()).limit(20).all()
    return render_template("dashboard.html", user=user, payments=payments)

# ─── Dashboard API ────────────────────────────────────────────────────────────

@app.route("/api/setup", methods=["POST"])
@login_required
def setup():
    user = User.query.get(session["user_id"])
    data = request.get_json() or request.form
    bakong_id = data.get("bakong_id", "").strip()
    shop_name = data.get("shop_name", "").strip()
    if not bakong_id or not shop_name:
        return jsonify({"success": False, "error": "Bakong ID និង Shop Name ត្រូវការ"}), 400
    user.bakong_id = bakong_id
    user.shop_name = shop_name
    if not user.api_key:
        user.api_key = gen_api_key()
    db.session.commit()
    return jsonify({"success": True, "api_key": user.api_key})

@app.route("/api/regenerate-key", methods=["POST"])
@login_required
def regenerate_key():
    user = User.query.get(session["user_id"])
    user.api_key = gen_api_key()
    db.session.commit()
    return jsonify({"success": True, "api_key": user.api_key})

@app.route("/api/me")
@login_required
def me():
    user = User.query.get(session["user_id"])
    return jsonify({
        "email":      user.email,
        "bakong_id":  user.bakong_id,
        "shop_name":  user.shop_name,
        "api_key":    user.api_key,
        "total_paid": user.total_paid,
    })

@app.route("/api/payments")
@login_required
def list_payments():
    user = User.query.get(session["user_id"])
    pmts = Payment.query.filter_by(user_id=user.id).order_by(Payment.created_at.desc()).limit(50).all()
    return jsonify({"success": True, "payments": [_pmt_dict(p) for p in pmts]})

def _pmt_dict(p):
    return {
        "pay_id":    p.pay_id,
        "amount":    p.amount,
        "currency":  p.currency,
        "note":      p.note,
        "status":    p.status,
        "qr_string": p.qr_string,
        "created_at": p.created_at.isoformat(),
        "paid_at":   p.paid_at.isoformat() if p.paid_at else None,
    }

# ─── Public KHQR API ──────────────────────────────────────────────────────────

@app.route("/api/v1/create-qr", methods=["POST"])
@api_key_required
def create_qr():
    user = request.api_user
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body ត្រូវការ"}), 400

    amount   = data.get("amount")
    currency = data.get("currency", "KHR")
    note     = data.get("note", "")
    expire   = int(data.get("expire_minutes", 10))
    webhook  = data.get("webhook_url", None)
    interval = int(data.get("poll_interval", 5))   # seconds between polls

    if not amount:
        return jsonify({"success": False, "error": "amount ត្រូវការ"}), 400
    try:
        amount = float(amount)
    except:
        return jsonify({"success": False, "error": "amount មិនត្រឹមត្រូវ"}), 400

    payload = {
        "bakongId":     user.bakong_id,
        "merchantName": user.shop_name,
        "amount":       amount,
        "currency":     currency,
        "note":         note,
    }

    try:
        r = requests.post(CAMRAPIDPAY_CREATE, json=payload, timeout=15)
        result = r.json()
    except Exception as e:
        return jsonify({"success": False, "error": f"CamRapidPay error: {str(e)}"}), 502

    # Save payment record
    pmt = Payment(
        user_id        = user.id,
        md5            = result.get("md5") or result.get("data", {}).get("md5"),
        transaction_id = result.get("transactionId") or result.get("data", {}).get("transactionId"),
        amount         = amount,
        currency       = currency,
        note           = note,
        qr_string      = result.get("qrString") or result.get("data", {}).get("qrString"),
        expire_minutes = expire,
        webhook_url    = webhook,
        status         = "PENDING",
    )
    db.session.add(pmt)
    db.session.commit()

    # Start background auto-poll
    start_poll(pmt.pay_id, interval=interval)

    return jsonify({
        "success":    True,
        "pay_id":     pmt.pay_id,
        "shop_name":  user.shop_name,
        "bakong_id":  user.bakong_id,
        "amount":     amount,
        "currency":   currency,
        "auto_poll":  True,
        "poll_interval": interval,
        "expire_minutes": expire,
        "data":       result,
    })

@app.route("/api/v1/payment-status/<pay_id>", methods=["GET"])
@api_key_required
def payment_status(pay_id):
    user = request.api_user
    pmt  = Payment.query.filter_by(pay_id=pay_id, user_id=user.id).first()
    if not pmt:
        return jsonify({"success": False, "error": "Payment not found"}), 404
    return jsonify({"success": True, **_pmt_dict(pmt)})

@app.route("/api/v1/cancel-payment/<pay_id>", methods=["POST"])
@api_key_required
def cancel_payment(pay_id):
    user = request.api_user
    pmt  = Payment.query.filter_by(pay_id=pay_id, user_id=user.id).first()
    if not pmt:
        return jsonify({"success": False, "error": "Payment not found"}), 404
    if pmt.status == "PENDING":
        pmt.status = "EXPIRED"
        db.session.commit()
        stop_poll(pay_id)
    return jsonify({"success": True, "status": pmt.status})

@app.route("/api/v1/check-payment", methods=["GET", "POST"])
@api_key_required
def check_payment():
    """Manual check — still available for backward compat."""
    user = request.api_user
    data = request.get_json() or {} if request.method == "POST" else request.args

    transaction_id = data.get("transaction_id") or data.get("transactionId")
    md5            = data.get("md5")
    if not transaction_id and not md5:
        return jsonify({"success": False, "error": "transaction_id ឬ md5 ត្រូវការ"}), 400

    params = {}
    if transaction_id: params["transactionId"] = transaction_id
    if md5:            params["md5"] = md5

    try:
        r      = requests.get(CAMRAPIDPAY_CHECK, params=params, timeout=15)
        result = r.json()
    except Exception as e:
        return jsonify({"success": False, "error": f"CamRapidPay error: {str(e)}"}), 502

    paid = (result.get("status") == "SUCCESS" or result.get("isPaid") is True)
    return jsonify({"success": True, "paid": paid, "data": result})

@app.route("/api/v1/info", methods=["GET"])
@api_key_required
def api_info():
    user = request.api_user
    return jsonify({
        "success":    True,
        "shop_name":  user.shop_name,
        "bakong_id":  user.bakong_id,
        "total_paid": user.total_paid,
    })

# ─── Init ─────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
