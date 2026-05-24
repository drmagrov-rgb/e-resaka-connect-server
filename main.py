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

# Structure :
# connected_users = {
#   "pseudo": {
#       "connections": {"device_id": websocket, ...},
#       "status": "online"
#   }
# }
connected_users = {}


def default_db():
    return {
        "profiles": {},
        "requests": [],
        "messages": [],
        "hidden_history": {}
    }


def normalize_db(data: dict):
    """Ajoute les nouvelles clés si db.json vient d'une ancienne version."""
    if not isinstance(data, dict):
        data = default_db()
    data.setdefault("profiles", {})
    data.setdefault("requests", [])
    data.setdefault("messages", [])
    data.setdefault("hidden_history", {})
    return data


def load_db():
    if not os.path.exists(DB_FILE):
        save_db(default_db())
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return normalize_db(json.load(f))
    except Exception:
        data = default_db()
        save_db(data)
        return data


def save_db(data):
    data = normalize_db(data)
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_username(username: str) -> str:
    return (username or "").strip()


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
        if (req.get("from") == a and req.get("to") == b) or (req.get("from") == b and req.get("to") == a):
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


def remove_relation(a: str, b: str):
    """Supprime uniquement quand un utilisateur clique sur Quitter discussion."""
    db = load_db()
    removed = False
    for req in list(db["requests"]):
        if (req.get("from") == a and req.get("to") == b) or (req.get("from") == b and req.get("to") == a):
            db["requests"].remove(req)
            removed = True
    if removed:
        save_db(db)
    return removed


def set_request(from_user: str, to_user: str, status: str):
    db = load_db()
    existing = None
    for req in db["requests"]:
        if (req.get("from") == from_user and req.get("to") == to_user) or (req.get("from") == to_user and req.get("to") == from_user):
            existing = req
            break

    # Annuler ne doit jamais supprimer une discussion déjà acceptée.
    # Une discussion acceptée reste active jusqu'au bouton "Quitter discussion".
    if status == "cancelled":
        if existing and existing.get("status") != "accepted":
            db["requests"].remove(existing)
            save_db(db)
        return existing.get("status") if existing else None

    # Ne jamais transformer une relation acceptée en demande en attente.
    if existing and existing.get("status") == "accepted" and status == "pending":
        return "accepted"

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
    return status

def requests_for_user(username: str):
    db = load_db()
    return [r for r in db["requests"] if r.get("from") == username or r.get("to") == username]


def save_message(sender: str, recipient: str, kind: str, message: str = "", filename: str = "", url: str = ""):
    db = load_db()
    rec = {
        "id": uuid.uuid4().hex,
        "type": kind,
        "from": sender,
        "to": recipient,
        "message": message or "",
        "filename": filename or "",
        "url": url or "",
        "created_at": now_text()
    }
    db["messages"].append(rec)

    # Sécurité simple : limiter la taille du fichier JSON.
    # Les 5000 derniers messages suffisent pour cette version de transition.
    if len(db["messages"]) > 5000:
        db["messages"] = db["messages"][-5000:]

    save_db(db)
    return rec


def history_key(other: str) -> str:
    return normalize_username(other)


def clear_history_for_user(viewer: str, other: str):
    db = load_db()
    db["hidden_history"].setdefault(viewer, {})[history_key(other)] = now_text()
    save_db(db)


def get_history(viewer: str, other: str, limit: int = 200):
    viewer = normalize_username(viewer)
    other = normalize_username(other)
    db = load_db()
    hidden_after = db.get("hidden_history", {}).get(viewer, {}).get(history_key(other), "")
    rows = []
    for msg in db.get("messages", []):
        participants_ok = (
            (msg.get("from") == viewer and msg.get("to") == other) or
            (msg.get("from") == other and msg.get("to") == viewer)
        )
        if not participants_ok:
            continue
        if hidden_after and msg.get("created_at", "") <= hidden_after:
            continue
        rows.append(msg)
    return rows[-max(1, min(limit, 1000)):]


def connected_usernames():
    return [u for u, info in connected_users.items() if info.get("connections")]


def user_payload_for(current_user: str):
    payload = []
    for username in connected_usernames():
        info = connected_users.get(username, {})
        p = get_profile(username)
        payload.append({
            **p,
            "status": info.get("status", "online"),
            "relation": "self" if username == current_user else relation_between(current_user, username)
        })
    return payload


async def send_json(username: str, payload: dict, exclude_device_id: str = None):
    info = connected_users.get(username)
    if not info:
        return
    dead_devices = []
    for device_id, websocket in list(info.get("connections", {}).items()):
        if exclude_device_id and device_id == exclude_device_id:
            continue
        try:
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            dead_devices.append(device_id)
    for device_id in dead_devices:
        info.get("connections", {}).pop(device_id, None)
    if not info.get("connections"):
        connected_users.pop(username, None)


async def send_state_to(username: str):
    await send_json(username, {"type": "users", "users": user_payload_for(username)})
    await send_json(username, {"type": "requests", "requests": requests_for_user(username)})


async def broadcast_users():
    for username in list(connected_usernames()):
        await send_state_to(username)


@app.get("/")
async def home():
    return {"application": "e-Res@ka Connect", "version": "4.1", "status": "actif"}


@app.get("/test")
async def test():
    return HTMLResponse("<h1>e-Res@ka Connect</h1><p>Serveur actif.</p><p>Version 4.1 - discussion persistante et notifications corrigées.</p>")


@app.get("/history/{username}/{other}")
async def history(username: str, other: str, limit: int = 200):
    username = normalize_username(username)
    other = normalize_username(other)
    if not username or not other:
        return {"messages": []}
    if not can_chat(username, other):
        return {"messages": []}
    return {"messages": get_history(username, other, limit)}


@app.post("/history/clear/{username}/{other}")
async def clear_history(username: str, other: str):
    username = normalize_username(username)
    other = normalize_username(other)
    if username and other:
        clear_history_for_user(username, other)
    return {"ok": True}


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
    device_id = uuid.uuid4().hex

    first_connection = False
    if not username_exists(username):
        first_connection = True
        save_profile(username, {"pseudo": username})

    if username not in connected_users:
        connected_users[username] = {"connections": {}, "status": "online"}
    connected_users[username]["connections"][device_id] = websocket

    await websocket.send_text(json.dumps({
        "type": "login_ok",
        "first_connection": first_connection,
        "profile": get_profile(username),
        "device_id": device_id
    }, ensure_ascii=False))

    await broadcast_users()

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            t = message.get("type")

            if t == "check_username":
                candidate = normalize_username(message.get("username", ""))
                await websocket.send_text(json.dumps({
                    "type": "username_check",
                    "username": candidate,
                    "exists": username_exists(candidate)
                }, ensure_ascii=False))

            elif t == "profile_update":
                save_profile(username, message.get("profile", {}))
                await broadcast_users()

            elif t == "status":
                status = message.get("status", "online")
                if status not in ["online", "busy", "away"]:
                    status = "online"
                if username in connected_users:
                    connected_users[username]["status"] = status
                await broadcast_users()

            elif t == "conversation_request":
                to_user = normalize_username(message.get("to", ""))
                if not to_user or to_user == username:
                    continue
                current_relation = relation_between(username, to_user)
                if current_relation == "accepted":
                    await websocket.send_text(json.dumps({
                        "type": "info",
                        "message": f"La discussion avec {to_user} est déjà active."
                    }, ensure_ascii=False))
                    await send_state_to(username)
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
                previous_status = set_request(username, to_user, "cancelled")
                if previous_status == "accepted":
                    await websocket.send_text(json.dumps({
                        "type": "info",
                        "message": "La discussion est déjà active. Utilisez Quitter discussion pour la terminer."
                    }, ensure_ascii=False))
                else:
                    await send_json(to_user, {"type": "conversation_cancelled", "from": username})
                await send_state_to(username)
                await send_state_to(to_user)
                await broadcast_users()

            elif t == "conversation_quit":
                to_user = normalize_username(message.get("to", ""))
                if not to_user or to_user == username:
                    continue
                remove_relation(username, to_user)
                await send_json(to_user, {
                    "type": "conversation_quit",
                    "from": username
                })
                await websocket.send_text(json.dumps({
                    "type": "info",
                    "message": f"Vous avez quitté la discussion avec {to_user}."
                }, ensure_ascii=False))
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

            elif t == "clear_history":
                other = normalize_username(message.get("with", ""))
                if other:
                    clear_history_for_user(username, other)
                    await websocket.send_text(json.dumps({
                        "type": "history_cleared",
                        "with": other
                    }, ensure_ascii=False))

            elif t == "private_message":
                recipient = normalize_username(message.get("to", ""))
                text = message.get("message", "")
                if not can_chat(username, recipient):
                    await websocket.send_text(json.dumps({"type": "error", "message": "Demande de conversation non acceptée."}, ensure_ascii=False))
                    continue
                rec = save_message(username, recipient, "text", message=text)
                await send_json(recipient, {
                    "type": "private_message",
                    "id": rec["id"],
                    "from": username,
                    "message": text,
                    "created_at": rec["created_at"]
                })
                # Synchronise les autres appareils du même compte, sans dupliquer sur l'appareil expéditeur.
                await send_json(username, {
                    "type": "own_message",
                    "id": rec["id"],
                    "to": recipient,
                    "message": text,
                    "created_at": rec["created_at"]
                }, exclude_device_id=device_id)

            elif t == "file_message":
                recipient = normalize_username(message.get("to", ""))
                filename = message.get("filename", "fichier")
                url = message.get("url", "")
                if not can_chat(username, recipient):
                    await websocket.send_text(json.dumps({"type": "error", "message": "Demande de conversation non acceptée."}, ensure_ascii=False))
                    continue
                rec = save_message(username, recipient, "file", filename=filename, url=url)
                await send_json(recipient, {
                    "type": "file_message",
                    "id": rec["id"],
                    "from": username,
                    "filename": filename,
                    "url": url,
                    "created_at": rec["created_at"]
                })
                await send_json(username, {
                    "type": "own_file_message",
                    "id": rec["id"],
                    "to": recipient,
                    "filename": filename,
                    "url": url,
                    "created_at": rec["created_at"]
                }, exclude_device_id=device_id)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("Erreur:", e)
    finally:
        info = connected_users.get(username)
        if info:
            info.get("connections", {}).pop(device_id, None)
            if not info.get("connections"):
                connected_users.pop(username, None)
        await broadcast_users()
