#!/usr/bin/env python3
"""换季衣物收纳管理系统 - Web 后台版"""
import os, json, sqlite3, shutil, io, base64
from datetime import date
from pathlib import Path
from flask import Flask, request, render_template, redirect, url_for, send_file, jsonify, flash

try:
    import qrcode
except ImportError:
    qrcode = None

BASE = Path.home() / "wardrobe-organizer"
DB_PATH = BASE / "webapp" / "data.db"
UPLOADS = BASE / "webapp" / "uploads"
QR_DIR = BASE / "webapp" / "static" / "qrcodes"
UPLOADS.mkdir(parents=True, exist_ok=True)
QR_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(BASE / "webapp" / "templates"),
            static_folder=str(BASE / "webapp" / "static"))
app.secret_key = "wardrobe-secret-key-change-in-production"

# ─── Database ───

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        );
        CREATE TABLE IF NOT EXISTS bags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL,
            label TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (date('now')),
            FOREIGN KEY (location_id) REFERENCES locations(id)
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bag_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            count INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (bag_id) REFERENCES bags(id)
        );
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            bag_id INTEGER,
            filename TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id),
            FOREIGN KEY (bag_id) REFERENCES bags(id)
        );
    """)
    conn.commit()
    conn.close()

def seed_demo():
    """导入现有的测试数据"""
    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    if existing > 0:
        conn.close()
        return
    data_file = BASE / "data.json"
    if not data_file.exists():
        conn.close()
        return
    data = json.loads(data_file.read_text())
    
    room_cache = {}
    loc_cache = {}
    
    for bag_data in data["bags"]:
        loc_str = bag_data.get("location", "")
        if not loc_str:
            continue
        
        # Parse room and spot
        if "（" in loc_str and "）" in loc_str:
            room_name, spot_name = loc_str.split("（")
            spot_name = spot_name.rstrip("）")
        else:
            room_name = loc_str
            spot_name = loc_str
        
        # Get or create room
        if room_name not in room_cache:
            conn.execute("INSERT OR IGNORE INTO rooms (name) VALUES (?)", (room_name,))
            conn.commit()
            r = conn.execute("SELECT id FROM rooms WHERE name = ?", (room_name,)).fetchone()
            room_cache[room_name] = r["id"]
        
        # Get or create location
        loc_key = f"{room_name}|{spot_name}"
        if loc_key not in loc_cache:
            conn.execute("INSERT INTO locations (room_id, name) VALUES (?, ?)",
                        (room_cache[room_name], spot_name))
            conn.commit()
            l = conn.execute("SELECT id FROM locations WHERE room_id = ? AND name = ?",
                           (room_cache[room_name], spot_name)).fetchone()
            loc_cache[loc_key] = l["id"]
        
        # Create bag
        cursor = conn.execute(
            "INSERT INTO bags (location_id, label, notes, created_at) VALUES (?, ?, ?, ?)",
            (loc_cache[loc_key], f"#{bag_data['id']:03d}", bag_data.get("notes", ""), bag_data.get("date", str(date.today())))
        )
        bag_id = cursor.lastrowid
        
        # Create items
        for idx, item in enumerate(bag_data["items"]):
            cursor = conn.execute(
                "INSERT INTO items (bag_id, name, count, sort_order) VALUES (?, ?, ?, ?)",
                (bag_id, item["name"], item["count"], idx)
            )
            item_db_id = cursor.lastrowid
            
            # Copy photos
            for p in item.get("photos", []):
                src = BASE / "photos" / p
                if src.exists():
                    # Copy to uploads
                    new_name = f"{item_db_id}_{p}"
                    shutil.copy2(src, UPLOADS / new_name)
                    conn.execute("INSERT INTO photos (item_id, filename) VALUES (?, ?)",
                               (item_db_id, new_name))
    
    conn.commit()
    conn.close()

# ─── Helper Functions ───

def get_room_tree():
    """获取完整的房间→位置→袋子→物品树"""
    conn = get_db()
    rooms = conn.execute("SELECT * FROM rooms ORDER BY sort_order, name").fetchall()
    result = []
    for room in rooms:
        locations = conn.execute(
            "SELECT * FROM locations WHERE room_id = ? ORDER BY sort_order, name",
            (room["id"],)
        ).fetchall()
        loc_list = []
        for loc in locations:
            bags = conn.execute(
                "SELECT * FROM bags WHERE location_id = ? ORDER BY id",
                (loc["id"],)
            ).fetchall()
            bag_list = []
            for bag in bags:
                items = conn.execute(
                    "SELECT * FROM items WHERE bag_id = ? ORDER BY sort_order",
                    (bag["id"],)
                ).fetchall()
                item_list = []
                for item in items:
                    photos = conn.execute(
                        "SELECT * FROM photos WHERE item_id = ?", (item["id"],)
                    ).fetchall()
                    item_list.append({**item, "photos": [dict(p) for p in photos]})
                bag_list.append({**bag, "items": item_list})
            loc_list.append({**loc, "bags": bag_list})
        result.append({**room, "locations": loc_list})
    conn.close()
    return result

def generate_qr(bag_id):
    """为指定袋子生成二维码"""
    if not qrcode:
        return None
    url = f"{request.host_url}bag/{bag_id}"
    qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#2c2c2c", back_color="white")
    path = QR_DIR / f"bag_{bag_id:04d}.png"
    img.save(path)
    return path

# ─── Routes ───

@app.route("/")
def index():
    tree = get_room_tree()
    total_bags = sum(len(r["locations"]) for r in tree)
    return render_template("index.html", tree=tree, total_bags=total_bags)

@app.route("/bag/<int:bag_id>")
def bag_detail(bag_id):
    conn = get_db()
    bag = conn.execute("""
        SELECT b.*, l.name as loc_name, r.name as room_name 
        FROM bags b JOIN locations l ON b.location_id = l.id 
        JOIN rooms r ON l.room_id = r.id WHERE b.id = ?
    """, (bag_id,)).fetchone()
    if not bag:
        return "袋子不存在", 404
    items = conn.execute("SELECT * FROM items WHERE bag_id = ? ORDER BY sort_order", (bag_id,)).fetchall()
    item_list = []
    for item in items:
        photos = conn.execute("SELECT * FROM photos WHERE item_id = ?", (item["id"],)).fetchall()
        item_list.append({**item, "photos": [dict(p) for p in photos]})
    conn.close()
    return render_template("bag_detail.html", bag=bag, items=item_list)

@app.route("/qr/<int:bag_id>")
def qr_download(bag_id):
    path = generate_qr(bag_id)
    if not path:
        flash("二维码组件未安装", "error")
        return redirect(url_for("index"))
    return send_file(str(path), mimetype="image/png", as_attachment=True, download_name=f"bag_{bag_id:04d}.png")

# ─── Room CRUD ───

@app.route("/rooms")
def manage_rooms():
    conn = get_db()
    rooms = conn.execute("SELECT r.*, COUNT(l.id) as loc_count FROM rooms r LEFT JOIN locations l ON r.id=l.room_id GROUP BY r.id ORDER BY r.sort_order, r.name").fetchall()
    conn.close()
    return render_template("rooms.html", rooms=rooms)

@app.route("/rooms/add", methods=["POST"])
def add_room():
    name = request.form.get("name", "").strip()
    if not name:
        flash("房间名称不能为空", "error")
    else:
        conn = get_db()
        try:
            conn.execute("INSERT INTO rooms (name) VALUES (?)", (name,))
            conn.commit()
        except sqlite3.IntegrityError:
            flash("该房间已存在", "error")
        conn.close()
    return redirect(url_for("manage_rooms"))

@app.route("/rooms/edit/<int:room_id>", methods=["POST"])
def edit_room(room_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_db()
        conn.execute("UPDATE rooms SET name = ? WHERE id = ?", (name, room_id))
        conn.commit()
        conn.close()
    return redirect(url_for("manage_rooms"))

@app.route("/rooms/delete/<int:room_id>")
def delete_room(room_id):
    conn = get_db()
    conn.execute("DELETE FROM photos WHERE item_id IN (SELECT i.id FROM items i JOIN bags b ON i.bag_id=b.id JOIN locations l ON b.location_id=l.id WHERE l.room_id=?)", (room_id,))
    conn.execute("DELETE FROM items WHERE bag_id IN (SELECT b.id FROM bags b JOIN locations l ON b.location_id=l.id WHERE l.room_id=?)", (room_id,))
    conn.execute("DELETE FROM bags WHERE location_id IN (SELECT id FROM locations WHERE room_id=?)", (room_id,))
    conn.execute("DELETE FROM locations WHERE room_id=?", (room_id,))
    conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("manage_rooms"))

# ─── Location CRUD ───

@app.route("/locations/<int:room_id>")
def manage_locations(room_id):
    conn = get_db()
    room = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    locations = conn.execute("SELECT l.*, COUNT(b.id) as bag_count FROM locations l LEFT JOIN bags b ON l.id=b.location_id WHERE l.room_id=? GROUP BY l.id ORDER BY l.sort_order, l.name", (room_id,)).fetchall()
    conn.close()
    return render_template("locations.html", room=room, locations=locations)

@app.route("/locations/add", methods=["POST"])
def add_location():
    room_id = request.form.get("room_id")
    name = request.form.get("name", "").strip()
    if name and room_id:
        conn = get_db()
        conn.execute("INSERT INTO locations (room_id, name) VALUES (?, ?)", (room_id, name))
        conn.commit()
        conn.close()
    return redirect(url_for("manage_locations", room_id=room_id))

@app.route("/locations/edit/<int:loc_id>", methods=["POST"])
def edit_location(loc_id):
    name = request.form.get("name", "").strip()
    room_id = request.form.get("room_id")
    if name:
        conn = get_db()
        conn.execute("UPDATE locations SET name = ? WHERE id = ?", (name, loc_id))
        conn.commit()
        conn.close()
    return redirect(url_for("manage_locations", room_id=room_id))

@app.route("/locations/delete/<int:loc_id>")
def delete_location(loc_id):
    conn = get_db()
    l = conn.execute("SELECT room_id FROM locations WHERE id=?", (loc_id,)).fetchone()
    room_id = l["room_id"] if l else 1
    conn.execute("DELETE FROM photos WHERE item_id IN (SELECT i.id FROM items i JOIN bags b ON i.bag_id=b.id WHERE b.location_id=?)", (loc_id,))
    conn.execute("DELETE FROM items WHERE bag_id IN (SELECT id FROM bags WHERE location_id=?)", (loc_id,))
    conn.execute("DELETE FROM bags WHERE location_id=?", (loc_id,))
    conn.execute("DELETE FROM locations WHERE id=?", (loc_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("manage_locations", room_id=room_id))

# ─── Bag CRUD ───

@app.route("/bags/<int:loc_id>")
def manage_bags(loc_id):
    conn = get_db()
    loc = conn.execute("SELECT l.*, r.name as room_name FROM locations l JOIN rooms r ON l.room_id=r.id WHERE l.id=?", (loc_id,)).fetchone()
    bags = conn.execute("SELECT * FROM bags WHERE location_id=? ORDER BY id", (loc_id,)).fetchall()
    bag_list = []
    for bag in bags:
        items = conn.execute("SELECT * FROM items WHERE bag_id=? ORDER BY sort_order", (bag["id"],)).fetchall()
        bag_list.append({**bag, "items": [dict(i) for i in items]})
    conn.close()
    return render_template("bags.html", loc=loc, bags=bag_list)

@app.route("/bags/add", methods=["POST"])
def add_bag():
    loc_id = request.form.get("loc_id")
    label = request.form.get("label", "").strip()
    notes = request.form.get("notes", "").strip()
    items_str = request.form.get("items", "").strip()
    if loc_id:
        conn = get_db()
        cursor = conn.execute("INSERT INTO bags (location_id, label, notes) VALUES (?, ?, ?)",
                            (loc_id, label, notes))
        bag_id = cursor.lastrowid
        # Parse items
        if items_str:
            for line in items_str.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "x" in line.lower():
                    name, cnt = line.lower().split("x", 1)
                    name = name.strip()
                    cnt = int(cnt.strip())
                else:
                    name = line
                    cnt = 1
                conn.execute("INSERT INTO items (bag_id, name, count) VALUES (?, ?, ?)",
                           (bag_id, name, cnt))
        conn.commit()
        conn.close()
        # Generate QR
        generate_qr(bag_id)
    return redirect(url_for("manage_bags", loc_id=loc_id))

@app.route("/bags/delete/<int:bag_id>")
def delete_bag(bag_id):
    conn = get_db()
    l = conn.execute("SELECT location_id FROM bags WHERE id=?", (bag_id,)).fetchone()
    loc_id = l["location_id"] if l else 1
    conn.execute("DELETE FROM photos WHERE item_id IN (SELECT id FROM items WHERE bag_id=?)", (bag_id,))
    conn.execute("DELETE FROM items WHERE bag_id=?", (bag_id,))
    conn.execute("DELETE FROM bags WHERE id=?", (bag_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("manage_bags", loc_id=loc_id))

@app.route("/items/photo/<int:item_id>", methods=["POST"])
def upload_photo(item_id):
    file = request.files.get("photo")
    if file:
        ext = os.path.splitext(file.filename)[1] or ".jpg"
        fname = f"{item_id}_{int(date.today().strftime('%Y%m%d'))}{ext}"
        file.save(str(UPLOADS / fname))
        conn = get_db()
        conn.execute("INSERT INTO photos (item_id, filename) VALUES (?, ?)", (item_id, fname))
        conn.commit()
        conn.close()
    return redirect(request.referrer or url_for("index"))

@app.route("/photo/<filename>")
def serve_photo(filename):
    path = UPLOADS / filename
    if path.exists():
        return send_file(str(path))
    return "", 404

# ─── Export QR codes ───

@app.route("/qr/print")
def qr_print():
    conn = get_db()
    bags = conn.execute("""
        SELECT b.id, b.label, l.name as loc_name, r.name as room_name,
        (SELECT GROUP_CONCAT(i.name || 'x' || i.count, '、') FROM items i WHERE i.bag_id=b.id) as items_summary
        FROM bags b JOIN locations l ON b.location_id=l.id JOIN rooms r ON l.room_id=r.id
        ORDER BY r.name, l.name, b.id
    """).fetchall()
    conn.close()
    # Generate any missing QR codes
    for bag in bags:
        qr_path = QR_DIR / f"bag_{bag['id']:04d}.png"
        if not qr_path.exists():
            generate_qr(bag["id"])
    return render_template("qr_print.html", bags=bags)

# ─── Main ───

if __name__ == "__main__":
    init_db()
    seed_demo()
    port = int(os.environ.get("PORT", 8765))
    print(f"\n🌐 换季衣物收纳管理系统")
    print(f"   启动地址: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port)
