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

# username -> {"websocket": websocket, "status": "online|busy|away"}
connected_users = {}


def default_db():
    return {
        "profiles": {},
        "requests": []
    }


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


def get_profile(username: str):
    db = load_db()
    profile = db["profiles"].get(username, {})
    return {
        "username": username,
        "pseudo": profile.get("pseudo", username),
        "full_name": profile.get("full_name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "photo_base64": profile.get("photo_base64", "")
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


def find_request(user_a: str, user_b: str):
    db = load_db()
    for req in db["requests"]:
        if (
            (req["from"] == user_a and req["to"] == user_b)
            or (req["from"] == user_b and req["to"] == user_a)
        ):
            return req
    return None


def can_chat(user_a: str, user_b: str) -> bool:
    req = find_request(user_a, user_b)
    return bool(req and req.get("status") == "accepted")


def set_request(from_user: str, to_user: str, status: str):
    db = load_db()
    existing = None

    for req in db["requests"]:
        if (
            (req["from"] == from_user and req["to"] == to_user)
            or (req["from"] == to_user and req["to"] == from_user)
        ):
            existing = req
            break

    if existing:
        existing["status"] = status
        existing["updated_at"] = now_text()

        # Quand on accepte une demande inversée, on garde le from/to original
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


def relation_between(current_user: str, other_user: str):
    req = find_request(current_user, other_user)

    if not req:
        return "none"

    if req.get("status") == "accepted":
        return "accepted"

    if req.get("status") == "rejected":
        return "rejected"

    if req.get("status") == "pending":
        if req.get("from") == current_user:
            return "pending_sent"
        return "pending_received"

    return "none"


def user_payload_for(current_user: str):
    data = []
    for username, info in connected_users.items():
        profile = get_profile(username)
        data.append({
            "username": username,
            "pseudo": profile.get("pseudo", username),
            "full_name": profile.get("full_name", ""),
            "email": profile.get("email", ""),
            "phone": profile.get("phone", ""),
            "photo_base64": profile.get("photo_base64", ""),
            "status": info.get("status", "online"),
            "relation": "self" if username == current_user else relation_between(current_user, username)
        })
    return data


def requests_for_user(username: str):
    db = load_db()
    return [
        req for req in db["requests"]
        if req.get("from") == username or req.get("to") == username
    ]


async def send_json(username: str, payload: dict):
    info = connected_users.get(username)
    if not info:
        return
    try:
        await info["websocket"].send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def send_state_to(username: str):
    await send_json(username, {
        "type": "users",
        "users": user_payload_for(username)
    })
    await send_json(username, {
        "type": "requests",
        "requests": requests_for_user(username)
    })


async def broadcast_users():
    for username in list(connected_users.keys()):
        await send_state_to(username)


@app.get("/")
async def home():
    return {
        "application": "e-Res@ka Connect",
        "status": "Serveur WebSocket actif",
        "profiles": "actif",
        "requests": "actif",
        "uploads": "actif"
    }


@app.get("/test")
async def test():
    return HTMLResponse("""
    <h1>e-Res@ka Connect</h1>
    <p>Serveur WebSocket actif.</p>
    <p>Profils, demandes de conversation et pièces jointes actifs.</p>
    """)


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    safe_original = os.path.basename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{safe_original}"
    save_path = os.path.join(UPLOAD_DIR, unique_name)

    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    return {
        "filename": safe_original,
        "stored_name": unique_name,
        "url": f"/download/{unique_name}"
    }


@app.get("/download/{stored_name}")
async def download_file(stored_name: str):
    file_path = os.path.join(UPLOAD_DIR, stored_name)

    if not os.path.exists(file_path):
        return {"error": "Fichier introuvable"}

    original_name = stored_name.split("_", 1)[1] if "_" in stored_name else stored_name

    return FileResponse(
        file_path,
        media_type="application/octet-stream",
        filename=original_name
    )


@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    username = normalize_username(username)

    await websocket.accept()

    # Empêche deux connexions actives avec le même pseudo
    if username in connected_users:
        try:
            await connected_users[username]["websocket"].close()
        except Exception:
            pass

    db = load_db()
    # Vérifie si le pseudo est déjà utilisé par un autre username
    for existing_user, existing_profile in db["profiles"].items():
        if existing_user != username:
            existing_pseudo = existing_profile.get("pseudo", existing_user)
            if existing_pseudo.strip().lower() == username.lower():
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Le pseudo '{username}' est déjà utilisé par un autre utilisateur. Veuillez choisir un autre pseudo."
                }))
                await websocket.close()
                return

    if username not in db["profiles"]:
        save_profile(username, {"pseudo": username})

    connected_users[username] = {
        "websocket": websocket,
        "status": "online"
    }

    print(f"{username} connecté.")
    await broadcast_users()

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)

            msg_type = message.get("type")

            if msg_type == "profile_update":
                profile_data = message.get("profile", {})
                new_pseudo = (profile_data.get("pseudo") or username).strip()

                # Vérifie unicité du pseudo parmi les autres utilisateurs
                db_check = load_db()
                pseudo_conflict = None
                for other_user, other_p in db_check["profiles"].items():
                    if other_user != username:
                        other_pseudo = other_p.get("pseudo", other_user).strip().lower()
                        if other_pseudo == new_pseudo.lower():
                            pseudo_conflict = True
                            break

                if pseudo_conflict:
                    await send_json(username, {
                        "type": "error",
                        "message": f"Le pseudo '{new_pseudo}' est déjà utilisé. Veuillez en choisir un autre."
                    })
                else:
                    save_profile(username, profile_data)
                    await broadcast_users()

            elif msg_type == "status":
                status = message.get("status", "online")

                if status not in ["online", "busy", "away"]:
                    status = "online"

                if username in connected_users:
                    connected_users[username]["status"] = status

                await broadcast_users()

            elif msg_type == "conversation_request":
                to_user = normalize_username(message.get("to", ""))

                if not to_user or to_user == username:
                    continue

                existing = find_request(username, to_user)

                if existing and existing.get("status") == "accepted":
                    await send_json(username, {
                        "type": "info",
                        "message": f"Vous pouvez déjà discuter avec {to_user}."
                    })
                else:
                    set_request(username, to_user, "pending")

                    await send_json(username, {
                        "type": "info",
                        "message": f"Demande envoyée à {to_user}."
                    })

                    await send_json(to_user, {
                        "type": "conversation_request",
                        "from": username,
                        "profile": get_profile(username)
                    })

                await send_state_to(username)
                await send_state_to(to_user)
                await broadcast_users()

            elif msg_type == "cancel_request":
                to_user = normalize_username(message.get("to", ""))

                if not to_user or to_user == username:
                    continue

                db_cancel = load_db()
                db_cancel["requests"] = [
                    r for r in db_cancel["requests"]
                    if not (
                        r.get("status") == "pending" and
                        (
                            (r["from"] == username and r["to"] == to_user) or
                            (r["from"] == to_user and r["to"] == username)
                        )
                    )
                ]
                save_db(db_cancel)

                await send_json(username, {
                    "type": "info",
                    "message": f"Demande annulée avec {to_user}."
                })

                await send_json(to_user, {
                    "type": "request_cancelled",
                    "from": username
                })

                await send_state_to(username)
                await send_state_to(to_user)
                await broadcast_users()

            elif msg_type == "conversation_response":
                from_user = normalize_username(message.get("from", ""))
                accepted = bool(message.get("accepted", False))

                if not from_user or from_user == username:
                    continue

                set_request(from_user, username, "accepted" if accepted else "rejected")

                await send_json(from_user, {
                    "type": "conversation_response",
                    "from": username,
                    "accepted": accepted
                })

                await send_json(username, {
                    "type": "info",
                    "message": "Demande acceptée." if accepted else "Demande refusée."
                })

                await send_state_to(username)
                await send_state_to(from_user)
                await broadcast_users()

            elif msg_type == "private_message":
                recipient = normalize_username(message.get("to", ""))
                text = message.get("message", "")

                if not can_chat(username, recipient):
                    await send_json(username, {
                        "type": "error",
                        "message": f"Vous devez d'abord avoir une demande acceptée avec {recipient}."
                    })
                    continue

                if recipient in connected_users:
                    await send_json(recipient, {
                        "type": "private_message",
                        "from": username,
                        "message": text
                    })
                else:
                    await send_json(username, {
                        "type": "error",
                        "message": f"{recipient} n'est pas connecté."
                    })

            elif msg_type == "file_message":
                recipient = normalize_username(message.get("to", ""))

                if not can_chat(username, recipient):
                    await send_json(username, {
                        "type": "error",
                        "message": f"Vous devez d'abord avoir une demande acceptée avec {recipient}."
                    })
                    continue

                if recipient in connected_users:
                    await send_json(recipient, {
                        "type": "file_message",
                        "from": username,
                        "filename": message.get("filename", "fichier"),
                        "url": message.get("url", "")
                    })
                else:
                    await send_json(username, {
                        "type": "error",
                        "message": f"{recipient} n'est pas connecté."
                    })

    except WebSocketDisconnect:
        connected_users.pop(username, None)
        print(f"{username} déconnecté.")
        await broadcast_users()

    except Exception as e:
        connected_users.pop(username, None)
        print(f"Erreur {username}:", e)
        await broadcast_users()
