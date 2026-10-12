import sqlite3, math, os
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "meditrack-secret-key"
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meditrack.db")
from catalog import CATALOG, USES
CATEGORIES = list(CATALOG)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, password TEXT, role TEXT DEFAULT 'user');
CREATE TABLE IF NOT EXISTS pharmacies(id INTEGER PRIMARY KEY, owner_id INTEGER, name TEXT, address TEXT, phone TEXT, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS medicines(id INTEGER PRIMARY KEY, name TEXT, generic TEXT, category TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY, pharmacy_id INTEGER, medicine_id INTEGER, price REAL, quantity INTEGER, UNIQUE(pharmacy_id, medicine_id));
CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, user_id INTEGER, pharmacy_id INTEGER, medicine_id INTEGER, qty INTEGER, status TEXT DEFAULT 'Reserved', created TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS favorites(user_id INTEGER, medicine_id INTEGER, PRIMARY KEY(user_id, medicine_id));
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, user_id INTEGER, pharmacy_id INTEGER, rating INTEGER, comment TEXT, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, pharmacy_id));
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER, text TEXT, link TEXT, is_read INTEGER DEFAULT 0, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS alerts(user_id INTEGER, medicine_id INTEGER, PRIMARY KEY(user_id, medicine_id));
"""

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e):
    d = g.pop("db", None)
    if d: d.close()

def seed_catalog(con):
    """Adds any medicine from catalog.py that is not in the DB yet (safe to run every time)."""
    pids = [r[0] for r in con.execute("SELECT id FROM pharmacies WHERE id<=4 OR owner_id IS NULL")]
    for cat, line in CATALOG.items():
        for it in line.split(";"):
            name, gen = it.split("|")
            if con.execute("SELECT 1 FROM medicines WHERE name=?", (name,)).fetchone(): continue
            m = con.execute("INSERT INTO medicines(name,generic,category,description) VALUES(?,?,?,?)",
                            (name, gen, cat, f"{gen}. {USES[cat]} Use as directed by a doctor or pharmacist.")).lastrowid
            for p in pids:
                if (p * 7 + m * 3) % 5 == 0: continue  # not every pharmacy stocks everything
                qty = 0 if (p + m) % 6 == 0 else (3 if (p + m) % 4 == 0 else 5 + (p * m) % 30)
                con.execute("INSERT INTO stock(pharmacy_id,medicine_id,price,quantity) VALUES(?,?,?,?)",
                            (p, m, round(8 + (m * 37) % 180 + p * 3, 2), qty))

def seed_stock_for_pharmacy(con, p):
    for m, in con.execute("SELECT id FROM medicines").fetchall():
        if (p * 7 + m * 3) % 5 == 0: continue
        qty = 0 if (p + m) % 6 == 0 else (3 if (p + m) % 4 == 0 else 5 + (p * m) % 30)
        con.execute("INSERT OR IGNORE INTO stock(pharmacy_id,medicine_id,price,quantity) VALUES(?,?,?,?)",
                    (p, m, round(8 + (m * 37) % 180 + p * 3, 2), qty))

def init_db():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM users").fetchone():
        for n, e, p, r in [("Demo User", "user@demo.com", "user123", "user"),
                           ("Owner One", "owner1@demo.com", "owner123", "owner"),
                           ("Owner Two", "owner2@demo.com", "owner123", "owner")]:
            con.execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)", (n, e, generate_password_hash(p), r))
        # Demo pharmacies (change lat/lon as you like)
        for o, n, a, ph, la, lo in [(2, "Sanjivani Medical", "Main Road, Market Area", "9800000001", 17.6800, 75.3300),
                                    (2, "Apollo Care Pharmacy", "Station Road", "9800000002", 17.6850, 75.3400),
                                    (3, "Jan Aushadhi Kendra", "Hospital Chowk", "9800000003", 17.6720, 75.3250),
                                    (3, "HealthPlus Chemist", "College Road", "9800000004", 17.6900, 75.3150)]:
            con.execute("INSERT INTO pharmacies(owner_id,name,address,phone,lat,lon) VALUES(?,?,?,?,?,?)", (o, n, a, ph, la, lo))
    if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        con.execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)", ("Admin", "admin@demo.com", generate_password_hash("admin123"), "admin"))
    seed_catalog(con)
    con.commit(); con.close()

def haversine(a, b, c, d):
    R = 6371; p1, p2 = math.radians(a), math.radians(c)
    x = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
    return R * 2 * math.asin(math.sqrt(x))

def with_dist(rows):
    loc, out = session.get("loc"), []
    for r in rows:
        d = dict(r)
        d["dist"] = round(haversine(loc[0], loc[1], d["lat"], d["lon"]), 1) if loc and d.get("lat") is not None else None
        out.append(d)
    return out

def status(q):
    return ("Out of Stock", "danger") if q <= 0 else (("Low Stock", "warning") if q <= 5 else ("Available", "success"))
app.jinja_env.globals["status"] = status
RX_CATS = {"Antibiotics", "Heart & BP"}
app.jinja_env.globals["is_rx"] = lambda c: c in RX_CATS
app.jinja_env.globals["rcode"] = lambda i: f"MT-{i:05d}"
app.jinja_env.globals["stars"] = lambda r: "★" * int(round(r or 0)) + "☆" * (5 - int(round(r or 0)))

@app.context_processor
def inject():
    favs = {r[0] for r in db().execute("SELECT medicine_id FROM favorites WHERE user_id=?", (session["uid"],))} if session.get("role") == "user" else set()
    ncount = db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (session["uid"],)).fetchone()[0] if "uid" in session else 0
    return dict(ncount=ncount, favs=favs, cu=session.get("name"), role=session.get("role"), categories=CATEGORIES, has_loc=bool(session.get("loc")), loc_label=session.get("loc_label", ""), radius=RADIUS_KM)

def login_required(role=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "uid" not in session:
                flash("Please login first.", "warning"); return redirect(url_for("login"))
            if role and session.get("role") != role:
                flash("Access denied.", "danger"); return redirect(url_for("index"))
            return f(*a, **k)
        return w
    return deco

@app.route("/")
def index():
    counts = {r["category"]: r["c"] for r in db().execute("SELECT category, COUNT(*) c FROM medicines GROUP BY category")}
    return render_template("index.html", counts=counts, npharm=db().execute("SELECT COUNT(*) FROM pharmacies").fetchone()[0])

RADIUS_KM = 10  # only pharmacies within this distance of the chosen location are shown

def nearby_ids():
    loc = session.get("loc")
    if not loc: return None
    return [r["id"] for r in db().execute("SELECT id,lat,lon FROM pharmacies").fetchall()
            if r["lat"] is not None and haversine(loc[0], loc[1], r["lat"], r["lon"]) <= RADIUS_KM]

@app.route("/set_location", methods=["POST"])
def set_location():
    d = request.get_json(force=True); lat, lon = float(d["lat"]), float(d["lon"])
    session["loc"] = [lat, lon]; session["loc_label"] = str(d.get("label", ""))[:60]
    con = db()
    for x in (d.get("pharmacies") or [])[:30]:  # real pharmacies found near this place (OpenStreetMap)
        try: la, lo = float(x["lat"]), float(x["lon"])
        except Exception: continue
        name = (x.get("name") or "Medical Store")[:80]
        if con.execute("SELECT 1 FROM pharmacies WHERE name=? AND abs(lat-?)<0.0005 AND abs(lon-?)<0.0005", (name, la, lo)).fetchone(): continue
        pid = con.execute("INSERT INTO pharmacies(owner_id,name,address,phone,lat,lon) VALUES(NULL,?,?,?,?,?)",
                          (name, (x.get("address") or session["loc_label"] or "Nearby")[:120], (x.get("phone") or "-")[:30], la, lo)).lastrowid
        seed_stock_for_pharmacy(con, pid)
    if not nearby_ids():  # nothing found online -> create a few sample pharmacies around this place
        for n, dy, dx in [("City Medical Store", .008, .004), ("Care Plus Pharmacy", -.006, .009), ("Health First Chemist", .004, -.010), ("Wellness Medicals", -.009, -.005)]:
            pid = con.execute("INSERT INTO pharmacies(owner_id,name,address,phone,lat,lon) VALUES(NULL,?,?,?,?,?)",
                              (n, f"Near {session['loc_label']} (sample)", "-", lat + dy, lon + dx)).lastrowid
            seed_stock_for_pharmacy(con, pid)
    con.commit()
    return jsonify(ok=True, count=len(nearby_ids()))

def med_rows(where="1=1", wargs=(), sort="", only_avail=False):
    ids = nearby_ids(); cond, args = "", []
    if ids is not None:
        cond, args = " AND s.pharmacy_id IN (%s)" % (",".join("?" * len(ids)) or "NULL"), ids
    order = {"price": "(minp IS NULL), minp", "avail": "avail DESC, m.name"}.get(sort, "m.name")
    having = " HAVING avail>0" if only_avail else ""
    return db().execute(f"""SELECT m.*, COUNT(CASE WHEN s.quantity>0 THEN 1 END) avail, MIN(CASE WHEN s.quantity>0 THEN s.price END) minp
        FROM medicines m LEFT JOIN stock s ON s.medicine_id=m.id{cond} WHERE {where} GROUP BY m.id{having} ORDER BY {order}""", (*args, *wargs)).fetchall()

@app.route("/search")
def search():
    q, cat, sort, only = request.args.get("q", "").strip(), request.args.get("category", ""), request.args.get("sort", ""), bool(request.args.get("avail"))
    rows = med_rows("(m.name LIKE ? OR m.generic LIKE ?) AND (?='' OR m.category=?)", (f"%{q}%", f"%{q}%", cat, cat), sort, only)
    return render_template("search.html", meds=rows, q=q, cat=cat, sort=sort, only=only)

@app.route("/api/suggest")
def suggest():
    q = request.args.get("q", "").strip()
    if len(q) < 2: return jsonify([])
    return jsonify([r[0] for r in db().execute("SELECT name FROM medicines WHERE name LIKE ? OR generic LIKE ? ORDER BY name LIMIT 8", (f"%{q}%", f"%{q}%"))])

@app.route("/favorite/<int:mid>")
@login_required("user")
def favorite(mid):
    con = db(); k = (session["uid"], mid)
    if con.execute("SELECT 1 FROM favorites WHERE user_id=? AND medicine_id=?", k).fetchone():
        con.execute("DELETE FROM favorites WHERE user_id=? AND medicine_id=?", k); flash("Removed from favorites.", "info")
    else:
        con.execute("INSERT INTO favorites VALUES(?,?)", k); flash("Added to favorites ❤️", "success")
    con.commit(); return redirect(request.referrer or url_for("favorites"))

@app.route("/favorites")
@login_required("user")
def favorites():
    return render_template("favorites.html", meds=med_rows("m.id IN (SELECT medicine_id FROM favorites WHERE user_id=?)", (session["uid"],)))

@app.route("/emergency")
def emergency():
    return render_template("emergency.html")

@app.route("/review", methods=["POST"])
@login_required("user")
def review():
    pid, rt = int(request.form["pharmacy_id"]), min(5, max(1, int(request.form["rating"])))
    db().execute("""INSERT INTO reviews(user_id,pharmacy_id,rating,comment) VALUES(?,?,?,?)
        ON CONFLICT(user_id,pharmacy_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment, created=CURRENT_TIMESTAMP""",
        (session["uid"], pid, rt, request.form.get("comment", "")[:300]))
    db().commit(); flash("Thanks for your review!", "success"); return redirect(url_for("pharmacy", pid=pid))

@app.route("/medicine/<int:mid>")
def medicine(mid):
    m = db().execute("SELECT * FROM medicines WHERE id=?", (mid,)).fetchone()
    if not m: return redirect(url_for("search"))
    rows = with_dist(db().execute("""SELECT s.*, p.name pname, p.address, p.phone, p.lat, p.lon FROM stock s
        JOIN pharmacies p ON p.id=s.pharmacy_id WHERE s.medicine_id=?""", (mid,)).fetchall())
    rows = [r for r in rows if r["dist"] is None or r["dist"] <= RADIUS_KM]
    rows.sort(key=lambda r: (r["quantity"] <= 0, r["price"]))
    cheapest = next((r["id"] for r in rows if r["quantity"] > 0), None)
    has_alert = bool(session.get("uid") and db().execute("SELECT 1 FROM alerts WHERE user_id=? AND medicine_id=?", (session["uid"], mid)).fetchone())
    return render_template("medicine.html", m=m, rows=rows, cheapest=cheapest, has_alert=has_alert, alts=med_rows("m.generic=? AND m.id<>?", (m["generic"], mid))[:6])

@app.route("/pharmacies")
def pharmacies():
    rows = [r for r in with_dist(db().execute("SELECT p.*, (SELECT ROUND(AVG(rating),1) FROM reviews WHERE pharmacy_id=p.id) rating, (SELECT COUNT(*) FROM reviews WHERE pharmacy_id=p.id) nrev FROM pharmacies p").fetchall()) if r["dist"] is None or r["dist"] <= RADIUS_KM]
    rows.sort(key=lambda r: (r["dist"] is None, r["dist"] or 0))
    return render_template("pharmacies.html", rows=rows)

@app.route("/pharmacy/<int:pid>")
def pharmacy(pid):
    p = db().execute("SELECT p.*, (SELECT ROUND(AVG(rating),1) FROM reviews WHERE pharmacy_id=p.id) rating, (SELECT COUNT(*) FROM reviews WHERE pharmacy_id=p.id) nrev FROM pharmacies p WHERE p.id=?", (pid,)).fetchone()
    if not p: return redirect(url_for("pharmacies"))
    items = db().execute("""SELECT s.*, m.name, m.category FROM stock s JOIN medicines m ON m.id=s.medicine_id
        WHERE s.pharmacy_id=? ORDER BY m.category, m.name""", (pid,)).fetchall()
    revs = db().execute("SELECT r.*, u.name uname FROM reviews r JOIN users u ON u.id=r.user_id WHERE r.pharmacy_id=? ORDER BY r.id DESC LIMIT 20", (pid,)).fetchall()
    return render_template("pharmacy.html", p=with_dist([p])[0], items=items, revs=revs)

@app.route("/reserve", methods=["POST"])
@login_required("user")
def reserve():
    sid, qty = int(request.form["stock_id"]), max(1, int(request.form.get("qty", 1)))
    con = db(); s = con.execute("SELECT * FROM stock WHERE id=?", (sid,)).fetchone()
    if not s or s["quantity"] < qty:
        flash("Sorry, requested quantity is not available.", "danger")
        return redirect(request.referrer or url_for("index"))
    con.execute("UPDATE stock SET quantity=quantity-? WHERE id=?", (qty, sid))
    con.execute("INSERT INTO reservations(user_id,pharmacy_id,medicine_id,qty) VALUES(?,?,?,?)",
                (session["uid"], s["pharmacy_id"], s["medicine_id"], qty))
    ow = con.execute("SELECT owner_id, name FROM pharmacies WHERE id=?", (s["pharmacy_id"],)).fetchone()
    if ow and ow["owner_id"]:
        con.execute("INSERT INTO notifications(user_id,text,link) VALUES(?,?,?)", (ow["owner_id"], f"📦 New reservation (qty {qty}) at {ow['name']}", "/owner"))
    con.commit(); flash("Medicine reserved! Visit the pharmacy to collect it.", "success")
    return redirect(url_for("my_reservations"))

@app.route("/my-reservations")
@login_required("user")
def my_reservations():
    rows = db().execute("""SELECT r.*, m.name mname, p.name pname, p.address, p.phone FROM reservations r
        JOIN medicines m ON m.id=r.medicine_id JOIN pharmacies p ON p.id=r.pharmacy_id
        WHERE r.user_id=? ORDER BY r.id DESC""", (session["uid"],)).fetchall()
    return render_template("reservations.html", rows=rows)

def set_res_status(rid, new, check_owner=False):
    con = db(); r = con.execute("SELECT r.*, p.owner_id FROM reservations r JOIN pharmacies p ON p.id=r.pharmacy_id WHERE r.id=?", (rid,)).fetchone()
    if not r or r["status"] != "Reserved": return False
    if check_owner and r["owner_id"] != session["uid"]: return False
    if not check_owner and r["user_id"] != session["uid"]: return False
    if new == "Cancelled":
        con.execute("UPDATE stock SET quantity=quantity+? WHERE pharmacy_id=? AND medicine_id=?", (r["qty"], r["pharmacy_id"], r["medicine_id"]))
    con.execute("UPDATE reservations SET status=? WHERE id=?", (new, rid)); con.commit(); return True

@app.route("/cancel/<int:rid>")
@login_required("user")
def cancel(rid):
    flash("Reservation cancelled." if set_res_status(rid, "Cancelled") else "Cannot cancel.", "info")
    return redirect(url_for("my_reservations"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        n, e, p, r = (request.form[k].strip() for k in ("name", "email", "password", "role"))
        if not (n and e and len(p) >= 4):
            flash("Fill all fields (password min 4 chars).", "danger")
        elif db().execute("SELECT 1 FROM users WHERE email=?", (e.lower(),)).fetchone():
            flash("Email already registered.", "danger")
        else:
            db().execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)",
                         (n, e.lower(), generate_password_hash(p), "owner" if r == "owner" else "user"))
            db().commit(); flash("Account created. Please login.", "success"); return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE email=?", (request.form["email"].strip().lower(),)).fetchone()
        if u and check_password_hash(u["password"], request.form["password"]):
            loc, lb = session.get("loc"), session.get("loc_label"); session.clear(); session.update(uid=u["id"], name=u["name"], role=u["role"])
            if loc: session["loc"], session["loc_label"] = loc, lb
            return redirect(url_for({"owner": "owner", "admin": "admin"}.get(u["role"], "index")))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))

# ---------- Pharmacy owner ----------
def my_pharmacy():
    return db().execute("SELECT * FROM pharmacies WHERE owner_id=?", (session["uid"],)).fetchone()

@app.route("/owner")
@login_required("owner")
def owner():
    p = my_pharmacy(); items = res = []
    if p:
        items = db().execute("SELECT s.*, m.name, m.category FROM stock s JOIN medicines m ON m.id=s.medicine_id WHERE s.pharmacy_id=? ORDER BY m.name", (p["id"],)).fetchall()
        res = db().execute("""SELECT r.*, m.name mname, u.name uname, u.email FROM reservations r JOIN medicines m ON m.id=r.medicine_id
            JOIN users u ON u.id=r.user_id WHERE r.pharmacy_id=? ORDER BY r.id DESC""", (p["id"],)).fetchall()
    return render_template("owner.html", p=p, items=items, res=res, stats=dict(total=len(items), low=sum(1 for i in items if 0 < i["quantity"] <= 5), out=sum(1 for i in items if i["quantity"] <= 0), pending=sum(1 for r in res if r["status"] == "Reserved"), done=sum(1 for r in res if r["status"] == "Collected")),
                           meds=db().execute("SELECT * FROM medicines ORDER BY name").fetchall())

@app.route("/owner/pharmacy", methods=["POST"])
@login_required("owner")
def owner_pharmacy():
    f = request.form; vals = (f["name"], f["address"], f["phone"], float(f["lat"] or 0), float(f["lon"] or 0))
    if my_pharmacy():
        db().execute("UPDATE pharmacies SET name=?,address=?,phone=?,lat=?,lon=? WHERE owner_id=?", vals + (session["uid"],))
    else:
        db().execute("INSERT INTO pharmacies(name,address,phone,lat,lon,owner_id) VALUES(?,?,?,?,?,?)", vals + (session["uid"],))
    db().commit(); flash("Pharmacy details saved.", "success"); return redirect(url_for("owner"))

@app.route("/owner/stock", methods=["POST"])
@login_required("owner")
def owner_stock():
    p, f = my_pharmacy(), request.form
    if not p: flash("Create your pharmacy first.", "warning"); return redirect(url_for("owner"))
    mid = f.get("medicine_id")
    if not mid:
        name = f.get("new_name", "").strip()
        if not name: flash("Select or enter a medicine.", "danger"); return redirect(url_for("owner"))
        ex = db().execute("SELECT id FROM medicines WHERE lower(name)=lower(?)", (name,)).fetchone()
        if ex: mid = ex["id"]
        else:
            mid = db().execute("INSERT INTO medicines(name,generic,category,description) VALUES(?,?,?,?)",
                (name, f.get("new_name"), f.get("category") or CATEGORIES[0], f.get("description", ""))).lastrowid
    old = db().execute("SELECT quantity FROM stock WHERE pharmacy_id=? AND medicine_id=?", (p["id"], mid)).fetchone()
    db().execute("""INSERT INTO stock(pharmacy_id,medicine_id,price,quantity) VALUES(?,?,?,?)
        ON CONFLICT(pharmacy_id,medicine_id) DO UPDATE SET price=excluded.price, quantity=excluded.quantity""",
        (p["id"], mid, float(f["price"]), int(f["quantity"])))
    if int(f["quantity"]) > 0 and (not old or old["quantity"] <= 0):  # tell users who asked to be notified
        mn = db().execute("SELECT name FROM medicines WHERE id=?", (mid,)).fetchone()["name"]
        for r in db().execute("SELECT user_id FROM alerts WHERE medicine_id=?", (mid,)).fetchall():
            db().execute("INSERT INTO notifications(user_id,text,link) VALUES(?,?,?)", (r["user_id"], f"🔔 {mn} is now available at {p['name']}", f"/medicine/{mid}"))
        db().execute("DELETE FROM alerts WHERE medicine_id=?", (mid,))
    db().commit(); flash("Stock updated.", "success"); return redirect(url_for("owner"))

@app.route("/owner/stock/<int:sid>/delete")
@login_required("owner")
def owner_stock_delete(sid):
    p = my_pharmacy()
    if p: db().execute("DELETE FROM stock WHERE id=? AND pharmacy_id=?", (sid, p["id"])); db().commit()
    flash("Item removed.", "info"); return redirect(url_for("owner"))

@app.route("/owner/reservation/<int:rid>/<action>")
@login_required("owner")
def owner_reservation(rid, action):
    new = "Collected" if action == "collect" else "Cancelled"
    r = db().execute("SELECT user_id FROM reservations WHERE id=?", (rid,)).fetchone()
    ok = set_res_status(rid, new, True)
    if ok and r:
        db().execute("INSERT INTO notifications(user_id,text,link) VALUES(?,?,?)", (r["user_id"], f"Your reservation {rcode(rid)} was marked {new}.", "/my-reservations")); db().commit()
    flash("Reservation updated." if ok else "Cannot update.", "info")
    return redirect(url_for("owner"))

# ---------- Alerts, notifications, profile, slip, admin ----------
@app.route("/alert/<int:mid>")
@login_required("user")
def alert(mid):
    con = db(); k = (session["uid"], mid)
    if con.execute("SELECT 1 FROM alerts WHERE user_id=? AND medicine_id=?", k).fetchone():
        con.execute("DELETE FROM alerts WHERE user_id=? AND medicine_id=?", k); flash("Alert removed.", "info")
    else:
        con.execute("INSERT INTO alerts VALUES(?,?)", k); flash("Done! We will notify you when this medicine becomes available 🔔", "success")
    con.commit(); return redirect(request.referrer or url_for("index"))

@app.route("/notifications")
@login_required()
def notifications():
    rows = db().execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (session["uid"],)).fetchall()
    db().execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (session["uid"],)); db().commit()
    return render_template("notifications.html", rows=rows)

@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    u = db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    if request.method == "POST":
        name, new = request.form["name"].strip() or u["name"], request.form.get("new_password", "")
        if new:
            if not check_password_hash(u["password"], request.form.get("old_password", "")):
                flash("Current password is wrong.", "danger"); return redirect(url_for("profile"))
            if len(new) < 4:
                flash("New password must be at least 4 characters.", "danger"); return redirect(url_for("profile"))
            db().execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(new), u["id"]))
        db().execute("UPDATE users SET name=? WHERE id=?", (name, u["id"])); db().commit()
        session["name"] = name; flash("Profile updated.", "success"); return redirect(url_for("profile"))
    return render_template("profile.html", u=u)

@app.route("/slip/<int:rid>")
@login_required("user")
def slip(rid):
    r = db().execute("""SELECT r.*, m.name mname, p.name pname, p.address, p.phone FROM reservations r
        JOIN medicines m ON m.id=r.medicine_id JOIN pharmacies p ON p.id=r.pharmacy_id WHERE r.id=? AND r.user_id=?""", (rid, session["uid"])).fetchone()
    return render_template("slip.html", r=r) if r else redirect(url_for("my_reservations"))

@app.route("/admin")
@login_required("admin")
def admin():
    n = lambda sql: db().execute(sql).fetchone()[0]
    stats = dict(users=n("SELECT COUNT(*) FROM users WHERE role='user'"), owners=n("SELECT COUNT(*) FROM users WHERE role='owner'"),
                 pharmacies=n("SELECT COUNT(*) FROM pharmacies"), medicines=n("SELECT COUNT(*) FROM medicines"), reservations=n("SELECT COUNT(*) FROM reservations"))
    return render_template("admin.html", stats=stats, users=db().execute("SELECT * FROM users ORDER BY id").fetchall(),
        pharms=db().execute("SELECT p.*, u.name oname FROM pharmacies p LEFT JOIN users u ON u.id=p.owner_id ORDER BY p.id").fetchall())

@app.route("/admin/medicine", methods=["POST"])
@login_required("admin")
def admin_medicine():
    f = request.form
    if f["name"].strip():
        db().execute("INSERT INTO medicines(name,generic,category,description) VALUES(?,?,?,?)", (f["name"].strip(), f.get("generic", "").strip(), f["category"], f.get("description", "")))
        db().commit(); flash("Medicine added. Pharmacy owners can now add stock for it.", "success")
    return redirect(url_for("admin"))

@app.route("/admin/delete/<kind>/<int:i>")
@login_required("admin")
def admin_delete(kind, i):
    c = db()
    if kind == "user":
        c.execute("DELETE FROM favorites WHERE user_id=?", (i,)); c.execute("DELETE FROM alerts WHERE user_id=?", (i,)); c.execute("DELETE FROM notifications WHERE user_id=?", (i,))
        c.execute("DELETE FROM reviews WHERE user_id=?", (i,)); c.execute("DELETE FROM users WHERE id=? AND role!='admin'", (i,))
    elif kind == "pharmacy":
        c.execute("DELETE FROM stock WHERE pharmacy_id=?", (i,)); c.execute("DELETE FROM reviews WHERE pharmacy_id=?", (i,)); c.execute("DELETE FROM pharmacies WHERE id=?", (i,))
    elif kind == "medicine":
        for t in ("stock", "favorites", "alerts"): c.execute(f"DELETE FROM {t} WHERE medicine_id=?", (i,))
        c.execute("DELETE FROM medicines WHERE id=?", (i,))
    c.commit(); flash("Deleted.", "info"); return redirect(url_for("admin"))

init_db()
if __name__ == "__main__":
    app.run(debug=True)
