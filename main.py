from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import json
import os
import uuid

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# username -> {"websocket": websocket, "status": "online|busy|away"}
connected_users = {}


@app.get("/")
async def home():
    return {
        "application": "e-Res@ka Connect",
        "status": "Serveur WebSocket actif",
        "uploads": "actif"
    }


@app.get("/test")
async def test():
    return HTMLResponse("""
    <h1>e-Res@ka Connect</h1>
    <p>Serveur WebSocket actif.</p>
    <p>Upload fichiers actif.</p>
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

    # Le nom original est après uuid_
    original_name = stored_name.split("_", 1)[1] if "_" in stored_name else stored_name

    return FileResponse(
        file_path,
        media_type="application/octet-stream",
        filename=original_name
    )


async def broadcast_users():
    users_data = []

    for username, info in connected_users.items():
        users_data.append({
            "username": username,
            "status": info.get("status", "online")
        })

    payload = {
        "type": "users",
        "users": users_data
    }

    disconnected = []

    for username, info in list(connected_users.items()):
        try:
            await info["websocket"].send_text(json.dumps(payload))
        except Exception:
            disconnected.append(username)

    for username in disconnected:
        connected_users.pop(username, None)


@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await websocket.accept()

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

            if message.get("type") == "private_message":
                recipient = message.get("to")
                text = message.get("message", "")

                if recipient in connected_users:
                    await connected_users[recipient]["websocket"].send_text(
                        json.dumps({
                            "type": "private_message",
                            "from": username,
                            "message": text
                        })
                    )

            elif message.get("type") == "file_message":
                recipient = message.get("to")

                if recipient in connected_users:
                    await connected_users[recipient]["websocket"].send_text(
                        json.dumps({
                            "type": "file_message",
                            "from": username,
                            "filename": message.get("filename", "fichier"),
                            "url": message.get("url", "")
                        })
                    )

            elif message.get("type") == "status":
                status = message.get("status", "online")

                if status not in ["online", "busy", "away"]:
                    status = "online"

                if username in connected_users:
                    connected_users[username]["status"] = status

                await broadcast_users()

    except WebSocketDisconnect:
        connected_users.pop(username, None)
        print(f"{username} déconnecté.")
        await broadcast_users()

    except Exception as e:
        connected_users.pop(username, None)
        print(f"Erreur {username}:", e)
        await broadcast_users()
