from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect, url_for, flash
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import sqlite3  # kept only for backward-compat error type guards; safe to remove later
import psycopg2
import psycopg2.extras
from psycopg2 import errors as pg_errors
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from datetime import datetime
import webbrowser
import threading
import time
import sys
import socket
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "CHANGE_THIS_SECRET_KEY_12345"  # CHANGE in production
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 1 GB
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =========================================================
# DATABASE (PostgreSQL / Neon)
# =========================================================
def _normalize_pg_url(url: str) -> str:
    """Strip channel_binding (unsupported by psycopg2/libpq) and force sslmode=require."""
    p = urlparse(url)
    q = parse_qs(p.query)
    q.pop("channel_binding", None)
    q["sslmode"] = ["require"]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Put your Neon connection string in .env, e.g.\n"
        "DATABASE_URL=postgresql://neondb_owner:npg_CjQkp8OIJ7AL@ep-autumn-water-b55r1zwk-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
    )
DATABASE_URL = _normalize_pg_url(DATABASE_URL)


def get_db():
    con = psycopg2.connect(DATABASE_URL)
    return con


def _dict_cursor(con):
    return con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    con = get_db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id       SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            file_id       TEXT PRIMARY KEY,
            filename      TEXT NOT NULL,
            original_name TEXT NOT NULL,
            upload_date   TEXT NOT NULL,
            size          BIGINT NOT NULL,
            uploader      TEXT DEFAULT 'public'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS site_content (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Default admin
    cur.execute("SELECT 1 FROM admin WHERE username=%s", ("admin",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO admin (username, password) VALUES (%s, %s)",
            ("admin", generate_password_hash("admin123")),
        )

    # Default site content
    defaults = {
        "site_title": "JATANJOT'S Cloud Storage",
        "site_subtitle": "Your files, anywhere",
        "site_badge": "● LIVE",
        "admin_badge": "👑 ADMIN",
        "upload_hint": "Drop files here or click to upload",
        "upload_max": "Maximum file size: 1GB",
        "footer_text": "Server running on http://localhost:5000",
        "files_heading": "📂 My Files",
        "empty_message": "No files uploaded yet",
        "empty_sub": "Upload your first file now!",
    }
    for k, v in defaults.items():
        cur.execute(
            "INSERT INTO site_content (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO NOTHING",
            (k, v),
        )

    con.commit()
    cur.close()
    con.close()


def get_content():
    con = get_db()
    cur = _dict_cursor(con)
    cur.execute("SELECT key, value FROM site_content")
    rows = cur.fetchall()
    cur.close()
    con.close()
    return {r["key"]: r["value"] for r in rows}


def set_content(key, value):
    con = get_db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO site_content (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )
    con.commit()
    cur.close()
    con.close()


def format_size(bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024.0:
            return f"{bytes:.1f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.1f} TB"


ALLOWED_EXTENSIONS = {
    'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx',
    'xls', 'xlsx', 'zip', 'rar', 'mp4', 'mp3', 'avi', 'json',
    'py', 'js', 'html', 'css', 'csv', 'xml', 'md', 'ppt', 'pptx',
    'mov', 'wmv', 'flv', 'mkv', 'exe', 'msi', 'dmg'
}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# =========================================================
# AUTH DECORATORS
# =========================================================
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required_api(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# =========================================================
# PUBLIC SITE
# =========================================================
@app.route('/')
def index():
    c = get_content()
    return render_template_string(MAIN_PAGE, c=c, is_admin=session.get("admin", False))


# =========================================================
# ADMIN LOGIN / LOGOUT
# =========================================================
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        con = get_db()
        cur = _dict_cursor(con)
        cur.execute("SELECT * FROM admin WHERE username=%s", (u,))
        row = cur.fetchone()
        cur.close()
        con.close()
        if row and check_password_hash(row["password"], p):
            session["admin"] = True
            session["admin_user"] = u
            flash("Welcome back, admin!")
            return redirect(url_for("admin_panel"))
        flash("Invalid username or password")
    return render_template_string(LOGIN_PAGE)


@app.route('/admin/logout')
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_user", None)
    flash("Logged out successfully")
    return redirect(url_for("index"))


# =========================================================
# ADMIN PANEL
# =========================================================
@app.route('/admin')
@admin_required
def admin_panel():
    con = get_db()
    cur_files = _dict_cursor(con)
    cur_files.execute("SELECT * FROM files ORDER BY upload_date DESC")
    files = cur_files.fetchall()
    cur_files.close()

    cur_content = _dict_cursor(con)
    cur_content.execute("SELECT * FROM site_content ORDER BY key")
    content_rows = cur_content.fetchall()
    cur_content.close()

    cur_admin = _dict_cursor(con)
    cur_admin.execute("SELECT id, username FROM admin")
    admins = cur_admin.fetchall()
    cur_admin.close()
    con.close()

    files_list = []
    for f in files:
        d = dict(f)
        d['size_human'] = format_size(d['size'])
        files_list.append(d)

    return render_template_string(
        ADMIN_PAGE,
        files=files_list,
        content_rows=content_rows,
        admins=admins,
        current_admin=session.get("admin_user", ""),
    )


@app.route('/admin/content', methods=['POST'])
@admin_required
def admin_update_content():
    for key in request.form:
        if key.startswith("content_"):
            set_content(key[8:], request.form[key])
    flash("Site content updated successfully")
    return redirect(url_for("admin_panel"))


@app.route('/admin/change_password', methods=['POST'])
@admin_required
def admin_change_password():
    old = request.form.get('old_password', '')
    new = request.form.get('new_password', '')
    u = session.get('admin_user')
    con = get_db()
    cur = _dict_cursor(con)
    cur.execute("SELECT * FROM admin WHERE username=%s", (u,))
    row = cur.fetchone()
    if not row or not check_password_hash(row["password"], old):
        cur.close()
        con.close()
        flash("Old password incorrect")
        return redirect(url_for("admin_panel"))
    if len(new) < 6:
        cur.close()
        con.close()
        flash("New password must be at least 6 characters")
        return redirect(url_for("admin_panel"))
    cur.close()
    cur2 = con.cursor()
    cur2.execute(
        "UPDATE admin SET password=%s WHERE username=%s",
        (generate_password_hash(new), u),
    )
    con.commit()
    cur2.close()
    con.close()
    flash("Password changed successfully")
    return redirect(url_for("admin_panel"))


@app.route('/admin/add_admin', methods=['POST'])
@admin_required
def admin_add_admin():
    u = request.form.get('new_username', '').strip()
    p = request.form.get('new_password', '')
    if not u or len(p) < 6:
        flash("Username required and password >= 6 chars")
        return redirect(url_for("admin_panel"))
    con = get_db()
    cur = con.cursor()
    try:
        cur.execute(
            "INSERT INTO admin (username, password) VALUES (%s, %s)",
            (u, generate_password_hash(p)),
        )
        con.commit()
        flash(f"Admin '{u}' added")
    except pg_errors.UniqueViolation:
        con.rollback()
        flash("Username already exists")
    finally:
        cur.close()
        con.close()
    return redirect(url_for("admin_panel"))


@app.route('/admin/remove_admin/<int:aid>', methods=['POST'])
@admin_required
def admin_remove_admin(aid):
    con = get_db()
    cur = _dict_cursor(con)
    cur.execute("SELECT username FROM admin WHERE id=%s", (aid,))
    row = cur.fetchone()
    if row and row["username"] == session.get("admin_user"):
        cur.close()
        con.close()
        flash("You cannot delete your own account")
        return redirect(url_for("admin_panel"))
    cur.close()
    cur2 = con.cursor()
    cur2.execute("DELETE FROM admin WHERE id=%s", (aid,))
    con.commit()
    cur2.close()
    con.close()
    flash("Admin removed")
    return redirect(url_for("admin_panel"))


# =========================================================
# API: FILES
# =========================================================
@app.route('/api/files', methods=['GET', 'OPTIONS'])
def get_files():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        con = get_db()
        cur = _dict_cursor(con)
        cur.execute("SELECT * FROM files ORDER BY upload_date DESC")
        rows = cur.fetchall()
        cur.close()
        con.close()

        files = []
        for r in rows:
            f = dict(r)
            path = os.path.join(UPLOAD_FOLDER, f['filename'])
            if os.path.exists(path):
                f['size'] = os.path.getsize(path)
                f['size_human'] = format_size(f['size'])
                f['id'] = f['file_id']
                files.append(f)
        return jsonify({'files': files}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload', methods=['POST', 'OPTIONS'])
def upload_file():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        if not allowed_file(file.filename):
            return jsonify({'error': 'File type not allowed'}), 400

        file_id = str(uuid.uuid4())
        original = file.filename
        unique = f"{file_id}_{original}"
        path = os.path.join(UPLOAD_FOLDER, unique)
        file.save(path)
        size = os.path.getsize(path)

        uploader = session.get("admin_user") or "public"

        con = get_db()
        cur = con.cursor()
        cur.execute(
            """INSERT INTO files
               (file_id, filename, original_name, upload_date, size, uploader)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (file_id, unique, original, datetime.now().isoformat(), size, uploader),
        )
        con.commit()
        cur.close()
        con.close()

        return jsonify({
            'message': 'File uploaded successfully',
            'file_id': file_id,
            'original_name': original,
        }), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/download/<file_id>', methods=['GET', 'OPTIONS'])
def download_file(file_id):
    if request.method == 'OPTIONS':
        return '', 200
    try:
        con = get_db()
        cur = _dict_cursor(con)
        cur.execute("SELECT * FROM files WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
        cur.close()
        con.close()
        if not row:
            return jsonify({'error': 'File not found'}), 404
        path = os.path.join(UPLOAD_FOLDER, row["filename"])
        if not os.path.exists(path):
            return jsonify({'error': 'File not found on server'}), 404
        return send_from_directory(
            UPLOAD_FOLDER, row["filename"],
            as_attachment=True, download_name=row["original_name"],
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete/<file_id>', methods=['DELETE', 'OPTIONS'])
def delete_file(file_id):
    if request.method == 'OPTIONS':
        return '', 200
    if not session.get("admin"):
        return jsonify({'error': 'Admin login required'}), 401
    try:
        con = get_db()
        cur = _dict_cursor(con)
        cur.execute("SELECT * FROM files WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            con.close()
            return jsonify({'error': 'File not found'}), 404

        path = os.path.join(UPLOAD_FOLDER, row["filename"])
        if os.path.exists(path):
            os.remove(path)

        cur.close()
        cur2 = con.cursor()
        cur2.execute("DELETE FROM files WHERE file_id=%s", (file_id,))
        con.commit()
        cur2.close()
        con.close()
        return jsonify({'message': 'File deleted successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =========================================================
# LOGIN PAGE
# =========================================================
LOGIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Login</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Segoe UI',sans-serif;min-height:100vh;display:flex;
       align-items:center;justify-content:center;
       background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:20px}
  .card{background:#fff;padding:40px;border-radius:20px;width:100%;max-width:420px;
        box-shadow:0 20px 60px rgba(0,0,0,.3);animation:fadeIn .5s ease}
  @keyframes fadeIn{from{opacity:0;transform:translateY(-20px)}to{opacity:1;transform:translateY(0)}}
  h1{color:#333;text-align:center;margin-bottom:10px}
  p.sub{text-align:center;color:#888;margin-bottom:25px;font-size:.95rem}
  input{width:100%;padding:14px;margin:8px 0;border:1px solid #ddd;border-radius:10px;font-size:1rem}
  input:focus{outline:none;border-color:#667eea}
  button{width:100%;padding:14px;margin-top:15px;background:linear-gradient(135deg,#667eea,#764ba2);
         color:#fff;border:none;border-radius:10px;font-size:1rem;font-weight:600;cursor:pointer}
  button:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(102,126,234,.5)}
  .flash{background:#fee;color:#b91c1c;padding:10px;border-radius:8px;margin-bottom:15px;text-align:center;font-size:.9rem}
  .hint{text-align:center;color:#999;font-size:.85rem;margin-top:20px}
  a.back{display:block;text-align:center;margin-top:15px;color:#667eea;text-decoration:none;font-size:.9rem}
</style>
</head>
<body>
  <div class="card">
    <h1>🔐 Admin Login</h1>
    <p class="sub">Cloud Storage Control Panel</p>
    {% with msgs = get_flashed_messages() %}
      {% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}
    {% endwith %}
    <form method="post">
      <input name="username" placeholder="Username" required autofocus>
      <input name="password" type="password" placeholder="Password" required>
      <button>Login</button>
    </form>
    <p class="hint">Default: admin / admin123</p>
    <a class="back" href="/">← Back to site</a>
  </div>
</body>
</html>
"""


# =========================================================
# ADMIN PANEL PAGE
# =========================================================
ADMIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Panel</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Segoe UI',sans-serif;background:#f4f6f8;color:#222;min-height:100vh}
  header{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:20px 30px;
         display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
  header h1{font-size:1.5rem}
  header a{color:#fff;text-decoration:none;background:rgba(255,255,255,.2);padding:8px 16px;
           border-radius:8px;font-weight:600;margin-left:8px;font-size:.9rem}
  header a:hover{background:rgba(255,255,255,.35)}
  main{max-width:1200px;margin:30px auto;padding:0 20px 60px}
  .flash{background:#d1fae5;color:#065f46;padding:12px;border-radius:8px;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:20px}
  .card{background:#fff;border-radius:14px;padding:22px;box-shadow:0 2px 10px rgba(0,0,0,.06)}
  .card h2{font-size:1.15rem;color:#333;margin-bottom:15px;
           border-bottom:2px solid #f0f0f0;padding-bottom:10px}
  label{display:block;font-size:.85rem;color:#555;margin:10px 0 4px;font-weight:600}
  input[type=text],input[type=password],textarea{width:100%;padding:10px;border:1px solid #ddd;
       border-radius:8px;font-family:inherit;font-size:.95rem}
  input:focus,textarea:focus{outline:none;border-color:#667eea}
  button{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;border:none;
         padding:10px 20px;border-radius:8px;cursor:pointer;font-weight:600;margin-top:12px;font-size:.9rem}
  button:hover{opacity:.9}
  button.danger{background:#ef4444}
  button.danger:hover{background:#dc2626}
  .file-row{display:flex;justify-content:space-between;align-items:center;
            padding:12px;border-bottom:1px solid #f0f0f0;gap:10px;flex-wrap:wrap}
  .file-row:last-child{border-bottom:none}
  .file-info{flex:1;min-width:200px}
  .file-name{font-weight:600;color:#333;word-break:break-all}
  .file-meta{font-size:.8rem;color:#888;margin-top:4px}
  .btn-sm{padding:6px 12px;font-size:.8rem;margin:0 0 0 6px}
  .empty{text-align:center;color:#aaa;padding:30px 0}
  .admin-row{display:flex;justify-content:space-between;align-items:center;
             padding:10px;border-bottom:1px solid #f0f0f0}
  .me{color:#10b981;font-size:.8rem;font-weight:600;margin-left:8px}
</style>
</head>
<body>
<header>
  <h1>👑 Admin Panel — {{ current_admin }}</h1>
  <div>
    <a href="/">🌐 View Site</a>
    <a href="/admin/logout">🚪 Logout</a>
  </div>
</header>

<main>
  {% with msgs = get_flashed_messages() %}
    {% for m in msgs %}<div class="flash">✅ {{ m }}</div>{% endfor %}
  {% endwith %}

  <div class="grid">

    <!-- FILES MANAGEMENT -->
    <div class="card" style="grid-column:1/-1">
      <h2>📁 Manage Files ({{ files|length }})</h2>
      {% if files %}
        {% for f in files %}
          <div class="file-row">
            <div class="file-info">
              <div class="file-name">{{ f.original_name }}</div>
              <div class="file-meta">
                {{ f.size_human }} • {{ f.upload_date[:19].replace('T',' ') }} • by {{ f.uploader }}
              </div>
            </div>
            <div>
              <a href="/api/download/{{ f.file_id }}"><button class="btn-sm">⬇️</button></a>
              <button class="btn-sm danger" onclick="deleteFile('{{ f.file_id }}')">🗑️</button>
            </div>
          </div>
        {% endfor %}
      {% else %}
        <div class="empty">No files uploaded yet</div>
      {% endif %}
    </div>

    <!-- SITE CONTENT EDITOR -->
    <div class="card">
      <h2>✏️ Edit Site Content</h2>
      <form method="post" action="/admin/content">
        {% for row in content_rows %}
          <label>{{ row['key'].replace('_',' ').title() }}</label>
          <input type="text" name="content_{{ row['key'] }}" value="{{ row['value'] }}">
        {% endfor %}
        <button>💾 Save All Content</button>
      </form>
    </div>

    <!-- PASSWORD CHANGE -->
    <div class="card">
      <h2>🔑 Change My Password</h2>
      <form method="post" action="/admin/change_password">
        <label>Current password</label>
        <input type="password" name="old_password" required>
        <label>New password (min 6 chars)</label>
        <input type="password" name="new_password" required minlength="6">
        <button>🔒 Update Password</button>
      </form>
    </div>

    <!-- ADMIN USERS -->
    <div class="card">
      <h2>👥 Admin Users</h2>
      {% for a in admins %}
        <div class="admin-row">
          <div>
            <strong>{{ a['username'] }}</strong>
            {% if a['username'] == current_admin %}<span class="me">(you)</span>{% endif %}
          </div>
          {% if a['username'] != current_admin %}
          <form method="post" action="/admin/remove_admin/{{ a['id'] }}" style="margin:0"
                onsubmit="return confirm('Remove admin {{ a['username'] }}?')">
            <button class="btn-sm danger" type="submit">Remove</button>
          </form>
          {% endif %}
        </div>
      {% endfor %}

      <form method="post" action="/admin/add_admin" style="margin-top:20px;border-top:1px solid #eee;padding-top:15px">
        <h3 style="font-size:1rem;margin-bottom:8px">➕ Add new admin</h3>
        <label>Username</label>
        <input type="text" name="new_username" required>
        <label>Password (min 6 chars)</label>
        <input type="password" name="new_password" required minlength="6">
        <button>Add Admin</button>
      </form>
    </div>

  </div>
</main>

<script>
async function deleteFile(id){
  if(!confirm('Delete this file permanently?')) return;
  const r = await fetch('/api/delete/'+id, {method:'DELETE'});
  if(r.ok){ location.reload(); }
  else{ const d = await r.json(); alert('Error: '+(d.error||'failed')); }
}
</script>
</body>
</html>
"""


# =========================================================
# MAIN PUBLIC PAGE
# =========================================================
MAIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ c.site_title }}</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;
       background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
       min-height:100vh; padding:20px; }
.container { max-width:1200px; margin:0 auto; background:white; border-radius:20px;
             padding:30px; box-shadow:0 20px 60px rgba(0,0,0,.3); animation:fadeIn .5s ease; }
@keyframes fadeIn { from{opacity:0;transform:translateY(-20px)} to{opacity:1;transform:translateY(0)} }
header { text-align:center; margin-bottom:40px; padding-bottom:20px; border-bottom:2px solid #f0f0f0; }
header h1 { font-size:2.5rem; color:#333; display:flex; align-items:center; justify-content:center;
            gap:10px; flex-wrap:wrap; }
.status-badge { background:#4CAF50; color:white; padding:2px 12px; border-radius:20px;
                font-size:.7rem; font-weight:600; letter-spacing:1px; animation:pulse 2s infinite; }
.admin-badge { background:#FF6B6B; color:white; padding:2px 12px; border-radius:20px;
               font-size:.7rem; font-weight:600; letter-spacing:1px; }
.admin-link { background:#667eea; color:white; padding:6px 14px; border-radius:20px;
              font-size:.85rem; text-decoration:none; font-weight:600; display:inline-block; }
@keyframes pulse { 0%{opacity:1} 50%{opacity:.6} 100%{opacity:1} }
header p { color:#666; font-size:1.1rem; }
.upload-section { display:flex; flex-direction:column; align-items:center; margin-bottom:30px; }
.upload-area { width:100%; min-height:200px; border:3px dashed #c4c4c4; border-radius:15px;
               display:flex; align-items:center; justify-content:center; cursor:pointer;
               transition:all .3s ease; background:#fafafa; margin-bottom:15px; }
.upload-area:hover, .upload-area.dragover { border-color:#667eea; background:#f0f3ff; transform:scale(1.01); }
.upload-content { text-align:center; padding:20px; }
.upload-icon { font-size:4rem; display:block; margin-bottom:10px; }
.upload-area h3 { color:#333; margin-bottom:5px; }
.upload-area p { color:#999; font-size:.9rem; }
.upload-btn { background:linear-gradient(135deg,#667eea,#764ba2); color:white; border:none;
              padding:12px 40px; border-radius:30px; font-size:1rem; font-weight:600;
              cursor:pointer; transition:all .3s ease; box-shadow:0 4px 15px rgba(102,126,234,.4); }
.upload-btn:hover { transform:translateY(-2px); box-shadow:0 6px 20px rgba(102,126,234,.5); }
.progress-container { margin:20px 0; padding:15px; background:#f8f9fa; border-radius:10px; display:none; }
.progress-bar { width:100%; height:20px; background:#e9ecef; border-radius:10px; overflow:hidden; }
.progress-fill { height:100%; background:linear-gradient(90deg,#667eea,#764ba2);
                 border-radius:10px; transition:width .3s ease; width:0%; }
.progress-text { text-align:center; margin-top:5px; color:#666; }
.stats { display:flex; gap:20px; margin:20px 0 30px 0; padding:20px; background:#f8f9fa; border-radius:10px; }
.stat-item { flex:1; text-align:center; }
.stat-label { display:block; color:#666; font-size:.9rem; margin-bottom:5px; }
.stat-value { display:block; font-size:1.5rem; font-weight:700; color:#333; }
.files-section h2 { color:#333; margin-bottom:20px; }
.files-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:20px; }
.file-card { background:white; border:1px solid #e9ecef; border-radius:12px; padding:20px;
             transition:all .3s ease; animation:slideUp .3s ease; }
@keyframes slideUp { from{opacity:0;transform:translateY(20px)} to{opacity:1;transform:translateY(0)} }
.file-card:hover { transform:translateY(-5px); box-shadow:0 5px 20px rgba(0,0,0,.1); border-color:#667eea; }
.file-icon { font-size:2.5rem; margin-bottom:10px; }
.file-name { font-weight:600; color:#333; margin-bottom:5px; word-break:break-all; }
.file-size { color:#666; font-size:.9rem; }
.file-date { color:#999; font-size:.8rem; margin:5px 0 15px 0; }
.file-actions { display:flex; gap:10px; }
.file-actions button { flex:1; padding:8px; border:none; border-radius:6px; cursor:pointer;
                       font-weight:600; transition:all .3s ease; }
.download-btn { background:#667eea; color:white; }
.download-btn:hover { background:#5a67d8; transform:scale(1.05); }
.delete-btn { background:#fc8181; color:white; }
.delete-btn:hover { background:#f56565; transform:scale(1.05); }
.empty-state { grid-column:1/-1; text-align:center; padding:60px 20px; color:#999; }
.empty-icon { font-size:4rem; display:block; margin-bottom:15px; }
.empty-sub { font-size:.9rem; color:#bbb; }
.modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%;
         background:rgba(0,0,0,.5); z-index:1000; align-items:center; justify-content:center; }
.modal-content { background:white; padding:30px; border-radius:15px; max-width:500px;
                 width:90%; position:relative; animation:slideIn .3s ease; }
@keyframes slideIn { from{transform:translateY(-50px);opacity:0} to{transform:translateY(0);opacity:1} }
.modal-close { position:absolute; top:15px; right:20px; font-size:2rem; cursor:pointer;
               color:#999; transition:color .3s ease; }
.modal-close:hover { color:#333; }
.modal-body { text-align:center; }
.btn-close-modal { margin-top:20px; padding:10px 30px; background:linear-gradient(135deg,#667eea,#764ba2);
                   color:white; border:none; border-radius:8px; cursor:pointer; font-weight:600; }
.btn-close-modal:hover { transform:scale(1.05); }
.server-status { background:#f8f9fa; padding:10px 20px; border-radius:8px; margin-top:20px;
                 text-align:center; color:#666; font-size:.9rem; }
.server-status .dot { display:inline-block.server-status .dot { display:inline-block;
                      width:10px; height:10px; background:#4CAF50; border-radius:50%;
                      margin-right:8px; animation:pulse 2s infinite; }
@media (max-width:768px) {
  .container { padding:20px; }
  header h1 { font-size:1.8rem; }
  .files-grid { grid-template-columns:1fr; }
  .stats { flex-direction:column; gap:10px; }
}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>
      ☁️ {{ c.site_title }}
      <span class="status-badge">{{ c.site_badge }}</span>
      {% if is_admin %}<span class="admin-badge">{{ c.admin_badge }}</span>{% endif %}
    </h1>
    <p>{{ c.site_subtitle }}</p>
    <div style="margin-top:15px">
      {% if is_admin %}
        <a class="admin-link" href="/admin">⚙️ Admin Panel</a>
        <a class="admin-link" href="/admin/logout" style="background:#ef4444">🚪 Logout</a>
      {% else %}
        <a class="admin-link" href="/admin/login">🔐 Admin Login</a>
      {% endif %}
    </div>
  </header>

  <div class="upload-section">
    <div class="upload-area" id="uploadArea">
      <div class="upload-content">
        <span class="upload-icon">📤</span>
        <h3>{{ c.upload_hint }}</h3>
        <p>{{ c.upload_max }}</p>
      </div>
    </div>
    <input type="file" id="fileInput" style="display:none" multiple>
    <button class="upload-btn" id="uploadBtn">Select Files</button>
    <div class="progress-container" id="progressContainer">
      <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
      <div class="progress-text" id="progressText">Uploading...</div>
    </div>
  </div>

  <div class="stats">
    <div class="stat-item">
      <span class="stat-label">Total Files</span>
      <span class="stat-value" id="statFiles">0</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Total Size</span>
      <span class="stat-value" id="statSize">0 B</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Status</span>
      <span class="stat-value" style="color:#4CAF50">Online</span>
    </div>
  </div>

  <section class="files-section">
    <h2>{{ c.files_heading }}</h2>
    <div class="files-grid" id="filesGrid"></div>
  </section>

  <div class="server-status">
    <span class="dot"></span>{{ c.footer_text }}
  </div>
</div>

<div class="modal" id="modal">
  <div class="modal-content">
    <span class="modal-close" onclick="closeModal()">&times;</span>
    <div class="modal-body" id="modalBody"></div>
    <button class="btn-close-modal" onclick="closeModal()">Close</button>
  </div>
</div>

<script>
const uploadArea = document.getElementById('uploadArea');
const fileInput  = document.getElementById('fileInput');
const uploadBtn  = document.getElementById('uploadBtn');
const progressContainer = document.getElementById('progressContainer');
const progressFill = document.getElementById('progressFill');
const progressText = document.getElementById('progressText');
const filesGrid = document.getElementById('filesGrid');
const statFiles = document.getElementById('statFiles');
const statSize = document.getElementById('statSize');

const IS_ADMIN = {{ 'true' if is_admin else 'false' }};

function getFileIcon(name){
  const ext = name.split('.').pop().toLowerCase();
  const map = {
    pdf:'📕', doc:'📘', docx:'📘', xls:'📗', xlsx:'📗', ppt:'📙', pptx:'📙',
    png:'🖼️', jpg:'🖼️', jpeg:'🖼️', gif:'🖼️',
    zip:'🗜️', rar:'🗜️',
    mp4:'🎬', avi:'🎬', mov:'🎬', mkv:'🎬', wmv:'🎬', flv:'🎬',
    mp3:'🎵',
    py:'🐍', js:'📜', html:'🌐', css:'🎨', json:'📋', csv:'📊', xml:'📋', md:'📝',
    txt:'📄'
  };
  return map[ext] || '📄';
}

function formatDate(iso){
  if(!iso) return '';
  return iso.slice(0,19).replace('T',' ');
}

async function loadFiles(){
  try{
    const r = await fetch('/api/files');
    const data = await r.json();
    renderFiles(data.files || []);
  }catch(e){
    console.error(e);
  }
}

function renderFiles(files){
  filesGrid.innerHTML = '';
  let total = 0;
  files.forEach(f => {
    total += f.size || 0;
    const card = document.createElement('div');
    card.className = 'file-card';
    card.innerHTML = `
      <div class="file-icon">${getFileIcon(f.original_name)}</div>
      <div class="file-name">${escapeHtml(f.original_name)}</div>
      <div class="file-size">${f.size_human || ''}</div>
      <div class="file-date">${formatDate(f.upload_date)}</div>
      <div class="file-actions">
        <button class="download-btn" onclick="location.href='/api/download/${f.file_id}'">⬇️ Download</button>
        ${IS_ADMIN ? `<button class="delete-btn" onclick="deleteFile('${f.file_id}')">🗑️ Delete</button>` : ''}
      </div>`;
    filesGrid.appendChild(card);
  });
  if(files.length === 0){
    filesGrid.innerHTML = `
      <div class="empty-state">
        <span class="empty-icon">📭</span>
        <h3>{{ c.empty_message }}</h3>
        <p class="empty-sub">{{ c.empty_sub }}</p>
      </div>`;
  }
  statFiles.textContent = files.length;
  statSize.textContent = humanSize(total);
}

function humanSize(bytes){
  const units = ['B','KB','MB','GB','TB'];
  let i = 0;
  while(bytes >= 1024 && i < units.length-1){ bytes /= 1024; i++; }
  return bytes.toFixed(1) + ' ' + units[i];
}

function escapeHtml(s){
  return (s||'').replace(/[&<>"']/g, m => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[m]));
}

async function deleteFile(id){
  if(!confirm('Delete this file permanently?')) return;
  try{
    const r = await fetch('/api/delete/' + id, {method:'DELETE'});
    if(r.ok){ loadFiles(); }
    else{ const d = await r.json(); alert('Error: ' + (d.error||'failed')); }
  }catch(e){ alert('Error: ' + e.message); }
}

function openModal(html){
  document.getElementById('modalBody').innerHTML = html;
  document.getElementById('modal').style.display = 'flex';
}
function closeModal(){
  document.getElementById('modal').style.display = 'none';
}

uploadArea.addEventListener('click', () => fileInput.click());
uploadBtn.addEventListener('click', () => fileInput.click());

uploadArea.addEventListener('dragover', e => {
  e.preventDefault();
  uploadArea.classList.add('dragover');
});
uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
uploadArea.addEventListener('drop', e => {
  e.preventDefault();
  uploadArea.classList.remove('dragover');
  if(e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
});

fileInput.addEventListener('change', () => {
  if(fileInput.files.length) uploadFiles(fileInput.files);
});

async function uploadFiles(files){
  for(const file of files){
    await uploadOne(file);
  }
  fileInput.value = '';
  loadFiles();
}

function uploadOne(file){
  return new Promise((resolve) => {
    const form = new FormData();
    form.append('file', file);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    progressContainer.style.display = 'block';
    progressFill.style.width = '0%';
    progressText.textContent = `Uploading ${file.name}...`;

    xhr.upload.onprogress = e => {
      if(e.lengthComputable){
        const pct = (e.loaded / e.total * 100).toFixed(1);
        progressFill.style.width = pct + '%';
        progressText.textContent = `${file.name} — ${pct}%`;
      }
    };
    xhr.onload = () => {
      progressContainer.style.display = 'none';
      if(xhr.status >= 200 && xhr.status < 300){
        progressText.textContent = 'Done';
      }else{
        let msg = 'Upload failed';
        try{ msg = JSON.parse(xhr.responseText).error || msg; }catch(_){}
        alert(`Failed to upload ${file.name}: ${msg}`);
      }
      resolve();
    };
    xhr.onerror = () => {
      progressContainer.style.display = 'none';
      alert(`Network error uploading ${file.name}`);
      resolve();
    };
    xhr.send(form);
  });
}

loadFiles();
</script>
</body>
</html>
"""


# =========================================================
# ENTRY POINT
# =========================================================
def _open_browser(url):
    time.sleep(1.2)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _find_free_port(preferred=5000):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", preferred))
            return preferred
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


if __name__ == "__main__":
    init_db()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    # Auto-open browser (only on local dev / when not disabled)
    if os.environ.get("NO_BROWSER", "0") != "1":
        threading.Thread(
            target=_open_browser,
            args=(f"http://localhost:{port}",),
            daemon=True,
        ).start()

    print(f"🚀 Starting server on http://localhost:{port}", file=sys.stderr)
    app.run(host=host, port=port, debug=debug, threaded=True)