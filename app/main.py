from flask import Flask, send_from_directory, jsonify, request, Response
import subprocess
import os
import json
import zipfile
import uuid
import shutil
import threading
import time
import re  # Add regex for capturing album/playlist name

app = Flask(__name__, static_folder='web')
BASE_DOWNLOAD_FOLDER = '/tmp/playlist-dl/app/downloads'
COOKIE_FOLDER = '/tmp/playlist-dl/config/cookie'
CONFIG_FOLDER = '/tmp/playlist-dl/config'
AUDIO_DOWNLOAD_PATH = os.getenv('AUDIO_DOWNLOAD_PATH', BASE_DOWNLOAD_FOLDER)
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
ADMIN_DOWNLOAD_PATH = AUDIO_DOWNLOAD_PATH  # default to .env path

config = {
    "use_cookie": "none"
}

sessions = {}

os.makedirs(BASE_DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(COOKIE_FOLDER, exist_ok=True)
os.makedirs(CONFIG_FOLDER, exist_ok=True)

@app.route('/')
def serve_index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(app.static_folder, path)

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session_id = str(uuid.uuid4())
        sessions[session_id] = username
        response = jsonify({"success": True})
        response.set_cookie('session', session_id)
        return response
    return jsonify({"success": False}), 401

def is_logged_in():
    session_id = request.cookies.get('session')
    return session_id in sessions

@app.route('/logout', methods=['POST'])
def logout():
    response = jsonify({"success": True})
    response.delete_cookie('session')  # Remove session cookie
    return response

@app.route('/check-login')
def check_login():
    is_logged_in_status = is_logged_in()
    return jsonify({"loggedIn": is_logged_in_status})

@app.route('/read-config')
def read_config():
    return jsonify({"success": True, "data": get_config()})

@app.route('/enable-cookie', methods=['POST'])
def enable_cookie():
    cookie = request.files['cookie']
    cookie.save(COOKIE_FOLDER + "/cookie")
    update_config('use_cookie', COOKIE_FOLDER + "/cookie")
    return jsonify({"success": True})

@app.route('/disable-cookie', methods=['POST'])
def diable_cookie():
    update_config('use_cookie', 'none')
    return jsonify({"success": True})


@app.route('/download')
def download_media():
    config = get_config()
    spotify_link = request.args.get('spotify_link')
    if not spotify_link:
        return jsonify({"status": "error", "output": "No link provided"}), 400

    session_id = str(uuid.uuid4())
    temp_download_folder = os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    os.makedirs(temp_download_folder, exist_ok=True)

    if "spotify" in spotify_link:
        command = [
            'spotdl',
            '--output', f"{temp_download_folder}/{{artist}}/{{album}}/{{title}}.{{output-ext}}",
            spotify_link
        ]
    else:
        command = ['yt-dlp']
        if config['use_cookie'] != 'none':
            command += ['--cookies', config['use_cookie']]
        command += ['-x', '--audio-format', 'mp3',
            '-o', f"{temp_download_folder}/%(uploader)s/%(album)s/%(title)s.%(ext)s",
            spotify_link]
    print(command)

    is_admin = is_logged_in()
    return Response(generate(is_admin, command, temp_download_folder, session_id), mimetype='text/event-stream')

def generate(is_admin, command, temp_download_folder, session_id):
    album_name = None
    try:
        print(f"🎧 Command being run: {' '.join(command)}")
        print(f"📁 Temp download folder: {temp_download_folder}")

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        for line in process.stdout:
            print(f"▶️ {line.strip()}")
            yield f"data: {line.strip()}\n\n"

            # Capture album name for zipping later
            match = re.search(r'Found \d+ songs in (.+?) \(', line)
            if match:
                album_name = match.group(1).strip()

        process.stdout.close()
        process.wait()

        if process.returncode != 0:
            yield f"data: Error: Download exited with code {process.returncode}.\n\n"
            return

        # Gather all downloaded audio files
        downloaded_files = []
        for root, _, files in os.walk(temp_download_folder):
            for file in files:
                full_path = os.path.join(root, file)
                print(f"📄 Found file: {full_path}")
                downloaded_files.append(full_path)

        valid_audio_files = [f for f in downloaded_files if f.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg'))]

        if not valid_audio_files:
            yield f"data: Error: No valid audio files found. Please check the link.\n\n"
            return

        # ✅ ADMIN HANDLING
        if is_admin:
            for file_path in valid_audio_files:
                filename = os.path.basename(file_path)

                if 'General Conference' in filename and '｜' in filename:
                    speaker_name = filename.split('｜')[0].strip()
                    target_path = os.path.join(ADMIN_DOWNLOAD_PATH, speaker_name, filename)
                    print(f"🚚 Moving GC file to: {target_path}")
                else:
                    relative_path = os.path.relpath(file_path, temp_download_folder)
                    target_path = os.path.join(ADMIN_DOWNLOAD_PATH, relative_path)
                    print(f"🚚 Moving to default admin path: {target_path}")

                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                try:
                    shutil.move(file_path, target_path)
                except Exception as move_error:
                    print(f"❌ Failed to move {file_path} to {target_path}: {move_error}")


            shutil.rmtree(temp_download_folder, ignore_errors=True)
            yield "data: Download completed. Files saved to server directory.\n\n"
            return  # ✅ Don’t try to serve/move anything else

        # ✅ PUBLIC USER HANDLING
        if len(valid_audio_files) > 1:
            zip_filename = f"{album_name}.zip" if album_name else "playlist.zip"
            zip_path = os.path.join(temp_download_folder, zip_filename)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path in valid_audio_files:
                    arcname = os.path.relpath(file_path, start=temp_download_folder)
                    zipf.write(file_path, arcname=arcname)

            yield f"data: DOWNLOAD: {session_id}/{zip_filename}\n\n"

        else:
            from urllib.parse import quote
            relative_path = os.path.relpath(valid_audio_files[0], start=temp_download_folder)
            encoded_path = quote(relative_path)
            yield f"data: DOWNLOAD: {session_id}/{encoded_path}\n\n"

            # Schedule cleanup of the temp folder
            threading.Thread(target=delayed_delete, args=(temp_download_folder,)).start()

    except Exception as e:
        yield f"data: Error: {str(e)}\n\n"


def delayed_delete(folder_path):
    time.sleep(300)
    shutil.rmtree(folder_path, ignore_errors=True)

def emergency_cleanup_container_downloads():
    print("🚨 Running backup cleanup in /app/downloads")
    for folder in os.listdir(BASE_DOWNLOAD_FOLDER):
        folder_path = os.path.join(BASE_DOWNLOAD_FOLDER, folder)
        try:
            shutil.rmtree(folder_path)
            print(f"🗑️ Cleaned: {folder_path}")
        except Exception as e:
            print(f"⚠️ Could not delete {folder_path}: {e}")

def schedule_emergency_cleanup(interval_seconds=3600):
    def loop():
        while True:
            time.sleep(interval_seconds)
            emergency_cleanup_container_downloads()

    threading.Thread(target=loop, daemon=True).start()

@app.route('/set-download-path', methods=['POST'])
def set_download_path():
    global ADMIN_DOWNLOAD_PATH
    if not is_logged_in():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json()
    new_path = data.get('path')

    if not new_path:
        return jsonify({"success": False, "message": "Path cannot be empty."}), 400

    # Optional: Validate the path, ensure it exists
    if not os.path.isdir(new_path):
        try:
            os.makedirs(new_path, exist_ok=True)
        except Exception as e:
            return jsonify({"success": False, "message": f"Cannot create path: {str(e)}"}), 500

    ADMIN_DOWNLOAD_PATH = new_path
    return jsonify({"success": True, "new_path": ADMIN_DOWNLOAD_PATH})


@app.route('/downloads/<session_id>/<path:filename>')
def serve_download(session_id, filename):
    session_download_folder = os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    full_path = os.path.join(session_download_folder, filename)

    print(f"📥 Requested filename: {filename}")
    print(f"📁 Resolved full path: {full_path}")

    if ".." in filename or filename.startswith("/"):
        return "Invalid filename", 400

    if not os.path.isfile(full_path):
        print("❌ File does not exist!")
        return "File not found", 404

    return send_from_directory(session_download_folder, filename, as_attachment=True)

def get_config():
    global config
    return config

def update_config(key, value):
    global config
    config[key] = value
    with open(CONFIG_FOLDER + "/config.json", 'w') as f:
        json.dump(config, f)

def init_config():
    global config
    with open(CONFIG_FOLDER + "/config.json") as f:
        local_config = json.load(f)

    # read configuration from local file
    for (key, value) in local_config.items():
        if key in config.keys() and value is not None:
            config[key] = value

schedule_emergency_cleanup()
if __name__ == '__main__':
    init_config()
    app.run(host='0.0.0.0', port=5000)

