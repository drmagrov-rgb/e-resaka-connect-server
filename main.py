from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
import json
import os
import uuid
from datetime import datetime

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_FILE = os.path.join(BASE_DIR, "db.json")

os.makedirs(UPLOAD_DIR, exist_ok=True)

connected_users = {}


def default_db():
    return {"profiles": {}, "requests": []}


def load_db():
    if not os.path.exists(DB_FILE):
        save_db(default_db())
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        data = default_db()
        save_db(data)
        return data


def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_username(username: str) -> str:
    return username.strip()


def username_exists(username: str) -> bool:
    db = load_db()
    return username in db["profiles"]


def get_profile(username: str):
    db = load_db()
    p = db["profiles"].get(username, {})
    return {
        "username": username,
        "pseudo": p.get("pseudo", username),
        "full_name": p.get("full_name", ""),
        "email": p.get("email", ""),
        "phone": p.get("phone", ""),
        "photo_base64": p.get("photo_base64", "")
    }


def save_profile(username: str, profile: dict):
    db = load_db()
    db["profiles"][username] = {
        "pseudo": profile.get("pseudo", username) or username,
        "full_name": profile.get("full_name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "photo_base64": profile.get("photo_base64", "")
    }
    save_db(db)


def find_request(a: str, b: str):
    db = load_db()
    for req in db["requests"]:
        if (req["from"] == a and req["to"] == b) or (req["from"] == b and req["to"] == a):
            return req
    return None


def can_chat(a: str, b: str):
    req = find_request(a, b)
    return bool(req and req.get("status") == "accepted")


def relation_between(current: str, other: str):
    req = find_request(current, other)
    if not req:
        return "none"
    if req.get("status") == "accepted":
        return "accepted"
    if req.get("status") == "rejected":
        return "rejected"
    if req.get("status") == "pending":
        return "pending_sent" if req.get("from") == current else "pending_received"
    return "none"


def set_request(from_user: str, to_user: str, status: str):
    db = load_db()
    existing = None
    for req in db["requests"]:
        if (req["from"] == from_user and req["to"] == to_user) or (req["from"] == to_user and req["to"] == from_user):
            existing = req
            break

    if status == "cancelled":
        if existing:
            db["requests"].remove(existing)
            save_db(db)
        return

    if existing:
        existing["status"] = status
        existing["updated_at"] = now_text()
        if status == "pending":
            existing["from"] = from_user
            existing["to"] = to_user
    else:
        db["requests"].append({
            "from": from_user,
            "to": to_user,
            "status": status,
            "created_at": now_text(),
            "updated_at": now_text()
        })
    save_db(db)


def requests_for_user(username: str):
    db = load_db()
    return [r for r in db["requests"] if r.get("from") == username or r.get("to") == username]


def user_payload_for(current_user: str):
    payload = []
    for username, info in connected_users.items():
        p = get_profile(username)
        payload.append({
            **p,
            "status": info.get("status", "online"),
            "relation": "self" if username == current_user else relation_between(current_user, username)
        })
    return payload


async def send_json(username: str, payload: dict):
    info = connected_users.get(username)
    if not info:
        return
    try:
        await info["websocket"].send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def send_state_to(username: str):
    await send_json(username, {"type": "users", "users": user_payload_for(username)})
    await send_json(username, {"type": "requests", "requests": requests_for_user(username)})


async def broadcast_users():
    for username in list(connected_users.keys()):
        await send_state_to(username)


@app.get("/")
async def home():
    return {"application": "e-Res@ka Connect", "status": "actif"}


@app.get("/test")
async def test():
    return HTMLResponse("<h1>e-Res@ka Connect</h1><p>Serveur actif.</p>")


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    safe_original = os.path.basename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{safe_original}"
    save_path = os.path.join(UPLOAD_DIR, unique_name)
    with open(save_path, "wb") as f:
        f.write(await file.read())
    return {"filename": safe_original, "stored_name": unique_name, "url": f"/download/{unique_name}"}


@app.get("/download/{stored_name}")
async def download_file(stored_name: str):
    path = os.path.join(UPLOAD_DIR, stored_name)
    if not os.path.exists(path):
        return {"error": "Fichier introuvable"}
    original = stored_name.split("_", 1)[1] if "_" in stored_name else stored_name
    return FileResponse(path, media_type="application/octet-stream", filename=original)


@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    username = normalize_username(username)
    await websocket.accept()

    first_connection = False
    if not username_exists(username):
        first_connection = True
        save_profile(username, {"pseudo": username})

    if username in connected_users:
        await websocket.send_text(json.dumps({
            "type": "login_error",
            "message": "Ce pseudo est déjà connecté sur un autre appareil."
        }, ensure_ascii=False))
        await websocket.close()
        return

    connected_users[username] = {"websocket": websocket, "status": "online"}
    await websocket.send_text(json.dumps({
        "type": "login_ok",
        "first_connection": first_connection,
        "profile": get_profile(username)
    }, ensure_ascii=False))

    await broadcast_users()

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            t = message.get("type")

            if t == "check_username":
                candidate = normalize_username(message.get("username", ""))
                await send_json(username, {
                    "type": "username_check",
                    "username": candidate,
                    "exists": username_exists(candidate)
                })

            elif t == "profile_update":
                save_profile(username, message.get("profile", {}))
                await broadcast_users()

            elif t == "status":
                status = message.get("status", "online")
                if status not in ["online", "busy", "away"]:
                    status = "online"
                connected_users[username]["status"] = status
                await broadcast_users()

            elif t == "conversation_request":
                to_user = normalize_username(message.get("to", ""))
                if not to_user or to_user == username:
                    continue
                set_request(username, to_user, "pending")
                await send_json(to_user, {
                    "type": "conversation_request",
                    "from": username,
                    "profile": get_profile(username)
                })
                await send_state_to(username)
                await send_state_to(to_user)
                await broadcast_users()

            elif t == "conversation_cancel":
                to_user = normalize_username(message.get("to", ""))
                set_request(username, to_user, "cancelled")
                await send_json(to_user, {"type": "conversation_cancelled", "from": username})
                await send_state_to(username)
                await send_state_to(to_user)
                await broadcast_users()

            elif t == "conversation_response":
                from_user = normalize_username(message.get("from", ""))
                accepted = bool(message.get("accepted", False))
                set_request(from_user, username, "accepted" if accepted else "rejected")
                await send_json(from_user, {
                    "type": "conversation_response",
                    "from": username,
                    "accepted": accepted
                })
                await send_state_to(username)
                await send_state_to(from_user)
                await broadcast_users()

            elif t == "private_message":
                recipient = normalize_username(message.get("to", ""))
                if not can_chat(username, recipient):
                    await send_json(username, {"type": "error", "message": "Demande de conversation non acceptée."})
                    continue
                await send_json(recipient, {
                    "type": "private_message",
                    "from": username,
                    "message": message.get("message", "")
                })

            elif t == "file_message":
                recipient = normalize_username(message.get("to", ""))
                if not can_chat(username, recipient):
                    await send_json(username, {"type": "error", "message": "Demande de conversation non acceptée."})
                    continue
                await send_json(recipient, {
                    "type": "file_message",
                    "from": username,
                    "filename": message.get("filename", "fichier"),
                    "url": message.get("url", "")
                })

    except WebSocketDisconnect:
        connected_users.pop(username, None)
        await broadcast_users()
    except Exception as e:
        print("Erreur:", e)
        connected_users.pop(username, None)
        await broadcast_users()
