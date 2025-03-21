from flask import Flask, send_from_directory, jsonify, request, Response
import subprocess
import os
import zipfile
import uuid
import shutil
import threading
import time
import re  # Add regex for capturing album/playlist name

app = Flask(__name__, static_folder='web')
BASE_DOWNLOAD_FOLDER = '/app/downloads'
AUDIO_DOWNLOAD_PATH = os.getenv('AUDIO_DOWNLOAD_PATH', BASE_DOWNLOAD_FOLDER)
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')

sessions = {}

os.makedirs(BASE_DOWNLOAD_FOLDER, exist_ok=True)

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


@app.route('/download')
def download_media():
    spotify_link = request.args.get('spotify_link')
    if not spotify_link:
        return jsonify({"status": "error", "output": "No link provided"}), 400

    session_id = str(uuid.uuid4())
    download_folder = AUDIO_DOWNLOAD_PATH if is_logged_in() else os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    os.makedirs(download_folder, exist_ok=True)

    # Set up command for Spotify links with spotdl
    if "spotify" in spotify_link:
        command = [
            'spotdl',
            '--output', f"{download_folder}/{{artist}}/{{album}}/{{track-number}} - {{title}}.{{output-ext}}",
            spotify_link
        ]

    # Set up command for YouTube links with yt-dlp
    else:
        command = [
            'yt-dlp', '-x', '--audio-format', 'mp3',
            '-o', f"{download_folder}/%(uploader)s/%(album)s/%(track_number)s - %(title)s.%(ext)s",
            spotify_link
        ]

    is_admin = is_logged_in()
    return Response(generate(is_admin, command, download_folder, session_id), mimetype='text/event-stream')

def generate(is_admin, command, download_folder, session_id):
    album_name = None
    try:
        print(f"🎧 Command being run: {' '.join(command)}")
        print(f"📁 Download folder: {download_folder}")

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        for line in process.stdout:
            print(f"▶️ {line.strip()}")
            yield f"data: {line.strip()}\n\n"

            match = re.search(r'Found \d+ songs in (.+?) \(', line)
            if match:
                album_name = match.group(1).strip()

        process.stdout.close()
        process.wait()

        if process.returncode == 0:
            downloaded_files = []
            for root, _, files in os.walk(download_folder):
                for file in files:
                    full_path = os.path.join(root, file)
                    print(f"✅ Found file: {full_path}")
                    downloaded_files.append(full_path)

            valid_audio_files = [f for f in downloaded_files if f.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg'))]

            if not valid_audio_files:
                print("⚠️ No valid audio files downloaded.")
                yield f"data: Error: No valid audio files found. Please check the link.\n\n"
                return

            if len(valid_audio_files) > 1 and not is_admin:
                zip_filename = f"{album_name}.zip" if album_name else "playlist.zip"
                zip_path = os.path.join(download_folder, zip_filename)
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for root, _, files in os.walk(download_folder):
                        for file in files:
                            if file.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg')):
                                file_path = os.path.join(root, file)
                                arcname = os.path.relpath(file_path, start=download_folder)
                                zipf.write(file_path, arcname=arcname)
                                print(f"📦 Added to zip: {arcname}")

                print(f"📤 Yielding zip download path: {session_id}/{zip_filename}")
                yield f"data: DOWNLOAD: {session_id}/{zip_filename}\n\n"

            elif valid_audio_files and not is_admin:
                relative_path = os.path.relpath(valid_audio_files[0], start=download_folder)
                print(f"📤 Yielding file download path: {session_id}/{relative_path}")
                from urllib.parse import quote
                encoded_path = quote(relative_path)
                print(f"📤 Yielding encoded path: {session_id}/{encoded_path}")
                yield f"data: DOWNLOAD: {session_id}/{encoded_path}\n\n"


            else:
                print("✅ Admin mode download complete.")
                yield "data: Download completed. Files saved to server directory.\n\n"

            if not is_admin:
                threading.Thread(target=delayed_delete, args=(download_folder,)).start()

        else:
            print(f"❌ Download command exited with code {process.returncode}")
            yield f"data: Error: Download exited with code {process.returncode}.\n\n"

    except Exception as e:
        print(f"💥 Exception in generate(): {str(e)}")
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

schedule_emergency_cleanup()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

