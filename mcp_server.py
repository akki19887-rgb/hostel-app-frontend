"""
HostelOM MCP server  —  ek alag seva, app se bilkul juda hua nahi.
=================================================================

Ye file `app.py` ka hissa NAHI hai aur usko chhuti bhi nahi. Ye Render par apni alag
service banegi. Agar ye band ho jaye, aapka app waise ka waisa chalta rahega —
guest booking karte rahenge, staff kaam karta rahega. Isiliye ise alag rakha hai.

Ye karta kya hai: Claude ko aapke apne data se seedha baat karne deta hai. "Aaj kitni
booking hui", "kaun si listing approval maang rahi hai", "Durg ke lead kis haal me hain"
— ye sawaal ab chat me poochhe ja sakte hain, aur zarurat ho to yahin se approve bhi
kiya ja sakta hai.

Suraksha — teen taale, teenon alag:
  1. RAASTA CHHUPA HUA HAI. URL me ek lamba गुप्त shabd hai (MCP_PATH_SECRET).
     Bina uske server 404 deta hai, kuch bhi nahi batata.
  2. SIRF ANTHROPIC KE SERVER. Anthropic ke IP ke alawa kisi aur se aaya request
     turant mana kar diya jata hai (MCP_ALLOW_CIDRS).
  3. LIKHNA BAND HAI JAB TAK AAP KHUD NA KHOLEN. MCP_WRITE_ENABLED=1 kiye bina
     koi bhi badlaav karne wala tool kaam nahi karega — sirf padhega.

Aur jo kabhi bahar nahi jata: Aadhaar ya kisi bhi ID ki photo ka rasta, registration
PDF ka rasta, dastkhat, payment screenshot, cheque/QR — in sab ko har jawab se hata
diya jata hai (REDACT_FIELDS). Guest ka phone number bhi dhaka rehta hai jab tak aap
MCP_SHOW_PHONES=1 na karein.

Chalane ke liye (Render):
    Build : pip install -r requirements.txt
    Start : gunicorn -k uvicorn.workers.UvicornWorker mcp_server:app --bind 0.0.0.0:$PORT
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import os
import re
import uuid
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

# ============================================================================
#  1.  Settings
# ============================================================================

PATH_SECRET = os.environ.get("MCP_PATH_SECRET", "").strip()
WRITE_ENABLED = os.environ.get("MCP_WRITE_ENABLED", "").strip() == "1"
SHOW_PHONES = os.environ.get("MCP_SHOW_PHONES", "").strip() == "1"
ACTOR_UID = os.environ.get("MCP_ACTOR_UID", "mcp-connector").strip()

# Anthropic ka apna egress range. "*" likhne se ye jaanch band ho jayegi — sirf
# apne computer par test karte waqt aisa kijiye, Render par kabhi nahi.
_DEFAULT_CIDRS = "160.79.104.0/21"
ALLOW_CIDRS_RAW = os.environ.get("MCP_ALLOW_CIDRS", _DEFAULT_CIDRS).strip()

BUCKET_NAME = os.environ.get("FIREBASE_STORAGE_BUCKET", "").strip()
SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")

if not PATH_SECRET or len(PATH_SECRET) < 24:
    raise SystemExit(
        "MCP_PATH_SECRET set kijiye — kam se kam 24 akshar ka koi random shabd. "
        "Yahi aapke server ka tala hai."
    )

MCP_PATH = "/mcp/" + PATH_SECRET

# ============================================================================
#  2.  Firebase
# ============================================================================

firestore_db = None
bucket = None
_fb_error = None

if SERVICE_ACCOUNT_JSON:
    try:
        import firebase_admin
        from firebase_admin import credentials as fb_credentials
        from firebase_admin import auth as fb_auth
        from firebase_admin import firestore as fb_firestore
        from firebase_admin import storage as fb_storage

        _cred = fb_credentials.Certificate(json.loads(SERVICE_ACCOUNT_JSON))
        firebase_admin.initialize_app(_cred, {"storageBucket": BUCKET_NAME})
        firestore_db = fb_firestore.client()
        if BUCKET_NAME:
            bucket = fb_storage.bucket()
        print("Firebase taiyar hai.")
    except Exception as e:  # pragma: no cover - only hit on a bad key
        _fb_error = f"{e.__class__.__name__}: {e}"
        print("Firebase shuru nahi hua:", _fb_error)
else:
    _fb_error = "FIREBASE_SERVICE_ACCOUNT_JSON set nahi hai."

try:
    from google.cloud.firestore_v1.base_query import FieldFilter
except Exception:  # pragma: no cover
    FieldFilter = None


def _where(q, field: str, op: str, value):
    """Works on both the old positional API and the newer FieldFilter one."""
    if FieldFilter is not None:
        return q.where(filter=FieldFilter(field, op, value))
    return q.where(field, op, value)


def _need_db():
    if firestore_db is None:
        raise RuntimeError("Firebase juda nahi hai: " + (_fb_error or "kaaran pata nahi"))
    return firestore_db


# ============================================================================
#  3.  Safety: what never leaves this server
# ============================================================================

# Ye khaane kisi bhi jawab me nahi jayenge. ID ki photo ka rasta bahar gaya to
# usse signed URL banaya ja sakta hai — isiliye rasta bhi utna hi sanvedansheel hai
# jitni photo khud.
REDACT_FIELDS = {
    "aadharPhotoUrl", "aadharPhotoBackUrl", "pdfUrl", "idPhotoPath", "signature",
    "screenshotUrl", "chequePhotoUrl", "qrCodeUrl", "accountNumber", "ifsc",
    "upiId", "aadharLast4", "idLast4", "aadharNumber", "fatherName", "motherName",
    "emergencyContact", "relativeContact", "bookingLocation", "address",
}

PHONE_FIELDS = {"phone", "guestPhone", "ownerPhone", "contactPhone"}


def _mask_phone(p: Any) -> Any:
    if SHOW_PHONES or not p:
        return p
    d = re.sub(r"\D", "", str(p))
    if len(d) < 6:
        return "XXXX"
    return d[:2] + "X" * (len(d) - 4) + d[-2:]


def _iso(v: Any) -> Any:
    """bookings.createdAt is a Timestamp on the Razorpay path and an ISO string
    everywhere else. Give the caller one shape so it can sort and compare."""
    if v is None or isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def clean(d: Any, depth: int = 0) -> Any:
    """Strip the private fields, mask phones, flatten timestamps. Applied to every
    single thing this server returns — there is no path that skips it."""
    if depth > 6:
        return "…"
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            if k in REDACT_FIELDS:
                continue
            if k in PHONE_FIELDS:
                out[k] = _mask_phone(v)
            else:
                out[k] = clean(v, depth + 1)
        return out
    if isinstance(d, list):
        return [clean(x, depth + 1) for x in d[:200]]
    return _iso(d)


def _write_guard():
    if not WRITE_ENABLED:
        raise PermissionError(
            "Badlaav karne wale tool abhi band hain. Render me is service par "
            "MCP_WRITE_ENABLED=1 kijiye, phir dobara koshish kijiye."
        )


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _audit(action: str, target: str = "", detail: str = ""):
    """Same collection and same keys as app.py's _audit, so the Admin console's
    audit screen shows these alongside everything else. actorRole is 'mcp' so it is
    always obvious which changes came from a chat and which from the app."""
    try:
        _need_db().collection("auditLog").add({
            "action": action,
            "actorUid": ACTOR_UID,
            "actorRole": "mcp",
            "target": target or "",
            "detail": (detail or "")[:500],
            "ip": "",
            "at": _now_iso(),
        })
    except Exception:
        pass


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40]
    return s or "hostel"


# ============================================================================
#  4.  The server
# ============================================================================

INSTRUCTIONS = """
Ye HostelOM ka apna data hai — ek hostel/PG booking platform (ho-om.in, Chhattisgarh).

Samajhne ki baatein:
- Listing ka status: pending_approval (nayi, approval ka intezaar), approved_2d (live —
  sirf yahi guest ko dikhti hai), approved (purani tarah se approve, guest ko NAHI
  dikhti — ise theek karwana chahiye), paused_by_admin (rok di gayi), rejected.
- Booking ka status: pending_payment_confirmation, paid_verified, checked_in,
  checked_out, no_show, cancellation_requested, cancelled, rejected.
- Lead ka status: new, contacted, agreed, refused, converted.
- Guest 20% advance online deta hai; baaki 80% hostel ko seedha, check-in par. Jo paisa
  'amount' me hai wahi HostelOM ka hai.
- Guest ki ID, PDF, dastkhat aur bank details is server se kabhi nahi aate. Agar koi
  maange to bata dijiye ki ye jaan-boojh kar band hai.
""".strip()

mcp = FastMCP(
    "HostelOM",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    streamable_http_path=MCP_PATH,
)


# ---------------------------------------------------------------- read tools

@mcp.tool()
def ping() -> dict:
    """Check that the connector is alive and see which switches are on.
    Firebase se kuch nahi maangta, isliye sabse pehle isi se jaanch kijiye."""
    return {
        "ok": True,
        "firebase": firestore_db is not None,
        "firebase_error": _fb_error,
        "storage": bucket is not None,
        "write_enabled": WRITE_ENABLED,
        "phones_masked": not SHOW_PHONES,
        "time": _now_iso(),
    }


@mcp.tool()
def pending_actions() -> dict:
    """Aaj aapko kya dekhna hai — ek hi jagah.
    Approval maang rahi listings, paisa jaanchne wali bookings, cancellation ki
    darkhwast, aur wo listings jo purane 'approved' par atki hain (guest ko nahi
    dikhtin). Din shuru karne ke liye yahi tool sabse kaam ka hai."""
    db = _need_db()
    out: dict[str, Any] = {}

    def _rows(coll, field, value, limit=25, fields=None):
        rows = []
        q = _where(db.collection(coll), field, "==", value).limit(limit)
        for d in q.stream():
            r = {"id": d.id, **(d.to_dict() or {})}
            if fields:
                r = {k: r.get(k) for k in fields if k in r}
                r["id"] = d.id
            rows.append(clean(r))
        return rows

    out["listings_waiting_approval"] = _rows(
        "businesses", "status", "pending_approval", 25,
        ["businessName", "city", "propertyType", "ownerId", "dateSubmitted"])
    out["listings_stuck_on_old_approved"] = _rows(
        "businesses", "status", "approved", 25,
        ["businessName", "city", "status"])
    out["payments_to_verify"] = _rows(
        "bookings", "status", "pending_payment_confirmation", 25,
        ["bookingId", "guestName", "hostelName", "amount", "createdAt", "utrRef"])
    out["cancellation_requests"] = _rows(
        "bookings", "status", "cancellation_requested", 25,
        ["bookingId", "guestName", "hostelName", "amount", "cancellationRequestedAt"])

    out["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    out["note"] = ("'listings_stuck_on_old_approved' wali listings guest ko nahi dikhtin — "
                   "unka status approved_2d hona chahiye.")
    return out


@mcp.tool()
def daily_summary(days: int = 1) -> dict:
    """Pichhle kuch dinon ka hisaab: kitni booking, kitna paisa, kaun se haal me.
    days=1 matlab aaj, days=7 matlab hafta."""
    db = _need_db()
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max(1, days))).date().isoformat()
    by_status: dict[str, int] = {}
    earned = 0.0
    stay_value = 0.0
    rows = []
    for d in db.collection("bookings").limit(1500).stream():
        b = d.to_dict() or {}
        created = str(_iso(b.get("createdAt")) or "")[:10]
        if created < cutoff:
            continue
        st = b.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        if st in ("paid_verified", "checked_in", "checked_out"):
            try:
                earned += float(b.get("amount") or 0)
                stay_value += float(b.get("totalStayAmount") or 0)
            except Exception:
                pass
        rows.append({"bookingId": b.get("bookingId"), "status": st,
                     "hostelName": b.get("hostelName"), "amount": b.get("amount"),
                     "createdAt": created})
    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return clean({
        "since": cutoff,
        "bookings_total": sum(by_status.values()),
        "bookings_by_status": by_status,
        "hostelom_earned": round(earned, 2),
        "total_stay_value": round(stay_value, 2),
        "recent": rows[:25],
    })


@mcp.tool()
def list_bookings(status: str | None = None, days: int = 30, limit: int = 25) -> dict:
    """Bookings dekhiye. status khaali chhodiye to sabhi aayengi.
    status: pending_payment_confirmation, paid_verified, checked_in, checked_out,
    no_show, cancellation_requested, cancelled, rejected."""
    db = _need_db()
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max(1, days))).date().isoformat()
    q = db.collection("bookings")
    if status:
        q = _where(q, "status", "==", status)
    rows = []
    for d in q.limit(1500).stream():
        b = {"docId": d.id, **(d.to_dict() or {})}
        if str(_iso(b.get("createdAt")) or "")[:10] < cutoff:
            continue
        rows.append(clean({k: b.get(k) for k in (
            "docId", "bookingId", "status", "guestName", "guestPhone", "hostelName",
            "propertyId", "amount", "totalStayAmount", "hostelBalanceDue", "createdAt",
            "paymentMethod", "utrRef", "source")}))
    rows.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
    return {"since": cutoff, "count": len(rows), "bookings": rows[:max(1, min(limit, 100))]}


@mcp.tool()
def get_booking(booking_id: str) -> dict:
    """Ek booking ka poora vivaran. bookingId (BK-XXXXXXXX) ya document id, dono chalte hain.
    Guest ki ID ya dastkhat isme nahi aayenge."""
    db = _need_db()
    doc = db.collection("bookings").document(booking_id).get()
    if doc.exists:
        return clean({"docId": doc.id, **(doc.to_dict() or {})})
    for d in _where(db.collection("bookings"), "bookingId", "==", booking_id).limit(1).stream():
        return clean({"docId": d.id, **(d.to_dict() or {})})
    return {"error": "Ye booking nahi mili: " + booking_id}


@mcp.tool()
def list_properties(status: str | None = None, city: str | None = None, limit: int = 25) -> dict:
    """Listings dekhiye.
    status: pending_approval, approved_2d (live), approved (purani, guest ko nahi dikhti),
    paused_by_admin, rejected."""
    db = _need_db()
    q = db.collection("businesses")
    if status:
        q = _where(q, "status", "==", status)
    if city:
        q = _where(q, "city", "==", city)
    rows = []
    for d in q.limit(max(1, min(limit, 100))).stream():
        b = d.to_dict() or {}
        rooms = b.get("roomsAndBeds") or []
        beds = sum(len(r.get("beds") or []) for r in rooms if isinstance(r, dict))
        rows.append(clean({
            "id": d.id,
            "businessName": b.get("businessName"),
            "status": b.get("status"),
            "city": b.get("city"),
            "propertyType": b.get("propertyType"),
            "genderType": (b.get("rules") or {}).get("genderType"),
            "rooms": len(rooms),
            "beds": beds,
            "hasCover": bool(b.get("coverPhotoUrl")),
            "verifiedByAdmin": b.get("verifiedByAdmin"),
            "ownerId": b.get("ownerId"),
            "dateSubmitted": b.get("dateSubmitted"),
        }))
    return {"count": len(rows), "properties": rows}


@mcp.tool()
def get_property(business_id: str) -> dict:
    """Ek listing ka poora haal — kamre, bed, rate, aur kahan photo kam hai.
    Listing live karne se pehle yahi dekhna chahiye."""
    db = _need_db()
    d = db.collection("businesses").document(business_id).get()
    if not d.exists:
        return {"error": "Ye listing nahi mili: " + business_id}
    b = d.to_dict() or {}
    rooms_out, missing = [], []
    for r in (b.get("roomsAndBeds") or []):
        if not isinstance(r, dict):
            continue
        photos = [p for p in (r.get("photos") or []) if p]
        beds = r.get("beds") or []
        rooms_out.append({
            "id": r.get("id"), "name": r.get("name"), "floor": r.get("floor"),
            "beds": len(beds),
            "available": sum(1 for x in beds if isinstance(x, dict) and x.get("status") == "available"),
            "photos": len(photos),
            "has360": bool(r.get("panorama360Url")),
            "prices": sorted({x.get("price") for x in beds if isinstance(x, dict) and x.get("price")}),
        })
        if len(photos) < 1:
            missing.append(r.get("name") or r.get("id"))
    return clean({
        "id": d.id,
        "businessName": b.get("businessName"),
        "status": b.get("status"),
        "live_to_guests": b.get("status") == "approved_2d",
        "city": b.get("city"), "pinAddress": b.get("pinAddress"),
        "propertyType": b.get("propertyType"),
        "rules": b.get("rules"),
        "coverPhoto": bool(b.get("coverPhotoUrl")),
        "floorMaps": list((b.get("floorMapUrls") or {}).keys()),
        "publishedFloors": b.get("publishedFloors"),
        "rooms": rooms_out,
        "rooms_without_photos": missing,
        "ownerId": b.get("ownerId"),
        "commissionPercent": b.get("commissionPercent"),
    })


@mcp.tool()
def list_leads(status: str | None = None, district: str | None = None,
               assigned_to: str | None = None, limit: int = 30) -> dict:
    """Leads dekhiye — wo hostel jinse baat karni hai.
    status: new, contacted, agreed, refused, converted."""
    db = _need_db()
    q = db.collection("leads")
    if status:
        q = _where(q, "status", "==", status)
    if district:
        q = _where(q, "district", "==", district)
    if assigned_to:
        q = _where(q, "assignedTo", "==", assigned_to)
    rows = []
    for d in q.limit(max(1, min(limit, 100))).stream():
        l = d.to_dict() or {}
        rows.append(clean({
            "id": d.id, "name": l.get("name"), "phone": l.get("phone"),
            "city": l.get("city"), "district": l.get("district"),
            "locality": l.get("locality"), "status": l.get("status"),
            "rating": l.get("rating"), "reviewCount": l.get("reviewCount"),
            "assignedName": l.get("assignedName"), "assignedTo": l.get("assignedTo"),
            "updatedAt": l.get("updatedAt"),
            "lastNote": ((l.get("contactLog") or [{}])[-1] or {}).get("note"),
        }))
    rows.sort(key=lambda r: str(r.get("updatedAt") or ""), reverse=True)
    return {"count": len(rows), "leads": rows}


@mcp.tool()
def staff_list() -> dict:
    """Aapke staff aur unka kaam. active=false matlab access band hai."""
    db = _need_db()
    rows = [clean({"uid": d.id, **(d.to_dict() or {})}) for d in db.collection("staffRoles").stream()]
    rows.sort(key=lambda r: (not r.get("active"), str(r.get("name") or "")))
    return {"count": len(rows), "staff": rows}


@mcp.tool()
def staff_activity(days: int = 7) -> dict:
    """Staff ne kya kaam kiya — field visits aur verifier reports.
    outcome: listed, follow_up, refused, revisit. recommend: approve, reject, needs_fix."""
    db = _need_db()
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max(1, days))).isoformat()
    visits, reports, by_person = [], [], {}
    for d in db.collection("staffVisits").limit(800).stream():
        v = d.to_dict() or {}
        if str(v.get("visitedAt") or "") < cutoff:
            continue
        visits.append(clean({k: v.get(k) for k in
                             ("staffName", "hostelName", "outcome", "note", "visitedAt",
                              "followUpOn", "followUpDone")}))
        n = v.get("staffName") or "?"
        by_person[n] = by_person.get(n, 0) + 1
    for d in db.collection("verifierReports").limit(400).stream():
        v = d.to_dict() or {}
        if str(v.get("visitedAt") or "") < cutoff:
            continue
        reports.append(clean({k: v.get(k) for k in
                              ("verifierName", "hostelName", "propertyId", "recommend",
                               "pinCorrect", "photosMatch", "bedCountMatch", "note", "visitedAt")}))
    visits.sort(key=lambda r: str(r.get("visitedAt") or ""), reverse=True)
    return {"since": cutoff[:10], "visits_per_person": by_person,
            "visits": visits[:40], "verifier_reports": reports[:20]}


@mcp.tool()
def follow_ups_due() -> dict:
    """Wo hostel jinpe dobara jaana tay hua tha aur tareekh nikal chuki hai.
    Field executive se poochhne layak list."""
    db = _need_db()
    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    due = []
    for d in db.collection("staffVisits").limit(1000).stream():
        v = d.to_dict() or {}
        if v.get("followUpDone"):
            continue
        f = v.get("followUpOn")
        if f and str(f) <= today:
            due.append(clean({k: v.get(k) for k in
                              ("staffName", "hostelName", "ownerName", "ownerPhone",
                               "outcome", "note", "followUpOn")}))
    due.sort(key=lambda r: str(r.get("followUpOn") or ""))
    return {"today": today, "count": len(due), "due": due[:40]}


@mcp.tool()
def search(query: str, limit: int = 15) -> dict:
    """Naam, sheher ya phone se dhoondhiye — listings, leads aur bookings, teenon me."""
    db = _need_db()
    q = (query or "").strip().lower()
    if len(q) < 2:
        return {"error": "Kam se kam 2 akshar likhiye."}
    qd = re.sub(r"\D", "", q)
    props, leads, books = [], [], []

    for d in db.collection("businesses").limit(600).stream():
        b = d.to_dict() or {}
        hay = " ".join(str(b.get(k) or "") for k in ("businessName", "city", "pinAddress")).lower()
        if q in hay:
            props.append(clean({"id": d.id, "businessName": b.get("businessName"),
                                "city": b.get("city"), "status": b.get("status")}))
        if len(props) >= limit:
            break
    for d in db.collection("leads").limit(1200).stream():
        l = d.to_dict() or {}
        hay = " ".join(str(l.get(k) or "") for k in
                       ("name", "city", "district", "locality", "address")).lower()
        phone_hit = bool(qd) and len(qd) >= 6 and qd in re.sub(r"\D", "", str(l.get("phone") or ""))
        if q in hay or phone_hit:
            leads.append(clean({"id": d.id, "name": l.get("name"), "city": l.get("city"),
                                "district": l.get("district"), "status": l.get("status"),
                                "phone": l.get("phone")}))
        if len(leads) >= limit:
            break
    for d in db.collection("bookings").limit(800).stream():
        b = d.to_dict() or {}
        hay = " ".join(str(b.get(k) or "") for k in ("guestName", "hostelName", "bookingId")).lower()
        phone_hit = bool(qd) and len(qd) >= 6 and qd in re.sub(r"\D", "", str(b.get("guestPhone") or ""))
        if q in hay or phone_hit:
            books.append(clean({"docId": d.id, "bookingId": b.get("bookingId"),
                                "guestName": b.get("guestName"), "status": b.get("status"),
                                "hostelName": b.get("hostelName"), "amount": b.get("amount")}))
        if len(books) >= limit:
            break
    return {"properties": props, "leads": leads, "bookings": books}


@mcp.tool()
def audit_log(limit: int = 30) -> dict:
    """Kisne kya badla — sabse naya pehle. actorRole 'mcp' matlab wo badlaav yahin
    chat se hua tha."""
    db = _need_db()
    rows = []
    try:
        from google.cloud.firestore_v1 import Query
        q = db.collection("auditLog").order_by("at", direction=Query.DESCENDING)
        q = q.limit(max(1, min(limit, 100)))
        rows = [clean({"id": d.id, **(d.to_dict() or {})}) for d in q.stream()]
    except Exception:
        rows = [clean({"id": d.id, **(d.to_dict() or {})})
                for d in db.collection("auditLog").limit(200).stream()]
        rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
        rows = rows[:limit]
    return {"count": len(rows), "entries": rows}


@mcp.tool()
def revenue(month: str | None = None) -> dict:
    """Ek mahine ka hisaab. month 'YYYY-MM' ke roop me dijiye; khaali chhodenge to
    chalu mahina. 'hostelom_earned' hi aapka paisa hai — baaki 80% hostel ka hai."""
    db = _need_db()
    m = (month or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")).strip()
    if not re.match(r"^\d{4}-\d{2}$", m):
        return {"error": "month ko YYYY-MM tarah likhiye, jaise 2026-09."}
    earned = 0.0
    stay = 0.0
    n = 0
    per_property: dict[str, float] = {}
    for d in db.collection("bookings").limit(3000).stream():
        b = d.to_dict() or {}
        if str(_iso(b.get("createdAt")) or "")[:7] != m:
            continue
        if b.get("status") not in ("paid_verified", "checked_in", "checked_out"):
            continue
        try:
            a = float(b.get("amount") or 0)
        except Exception:
            a = 0.0
        earned += a
        n += 1
        try:
            stay += float(b.get("totalStayAmount") or 0)
        except Exception:
            pass
        key = b.get("hostelName") or b.get("propertyId") or "?"
        per_property[key] = round(per_property.get(key, 0.0) + a, 2)
    top = sorted(per_property.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return {"month": m, "paid_bookings": n, "hostelom_earned": round(earned, 2),
            "total_stay_value": round(stay, 2), "top_properties": dict(top)}


# ---------------------------------------------------------------- write tools

@mcp.tool()
def approve_property(business_id: str) -> dict:
    """Listing ko live kijiye (status approved_2d). Guest ko sirf yahi status dikhta hai.
    Pehle get_property se dekh lijiye ki photo aur bed theek hain."""
    _write_guard()
    db = _need_db()
    ref = db.collection("businesses").document(business_id)
    d = ref.get()
    if not d.exists:
        return {"error": "Ye listing nahi mili: " + business_id}
    b = d.to_dict() or {}
    ref.set({"status": "approved_2d", "verifiedAt": _now_iso()}, merge=True)
    _audit("mcp_property_approved", business_id, b.get("businessName") or "")
    return {"ok": True, "id": business_id, "businessName": b.get("businessName"),
            "status": "approved_2d", "message": "Ab ye listing guest ko dikhegi."}


@mcp.tool()
def pause_property(business_id: str, resume: bool = False) -> dict:
    """Listing ko rok dijiye (paused_by_admin) ya dobara chalu kijiye (resume=True)."""
    _write_guard()
    db = _need_db()
    ref = db.collection("businesses").document(business_id)
    if not ref.get().exists:
        return {"error": "Ye listing nahi mili: " + business_id}
    new_status = "approved_2d" if resume else "paused_by_admin"
    ref.set({"status": new_status}, merge=True)
    _audit("mcp_property_resumed" if resume else "mcp_property_paused", business_id, new_status)
    return {"ok": True, "id": business_id, "status": new_status}


@mcp.tool()
def update_lead(lead_id: str, status: str | None = None, note: str | None = None) -> dict:
    """Lead ka haal badliye aur note jodiye.
    status: new, contacted, agreed, refused. ('converted' yahan se nahi hota — wo tabhi
    lagta hai jab listing sach me ban jaye.)"""
    _write_guard()
    db = _need_db()
    ref = db.collection("leads").document(lead_id)
    d = ref.get()
    if not d.exists:
        return {"error": "Ye lead nahi mili: " + lead_id}
    if status and status not in ("new", "contacted", "agreed", "refused"):
        return {"error": "status new / contacted / agreed / refused me se koi ek hona chahiye."}
    payload: dict[str, Any] = {"updatedAt": _now_iso()}
    if status:
        payload["status"] = status
    log = list((d.to_dict() or {}).get("contactLog") or [])
    if note or status:
        log.append({"at": _now_iso(), "by": ACTOR_UID,
                    "note": (note or "")[:500], "status": status or (d.to_dict() or {}).get("status")})
        payload["contactLog"] = log[-50:]
    ref.set(payload, merge=True)
    _audit("mcp_lead_update", lead_id, (status or "") + " " + (note or "")[:120])
    return {"ok": True, "id": lead_id, "status": status or (d.to_dict() or {}).get("status")}


@mcp.tool()
def assign_lead(lead_id: str, staff_uid: str) -> dict:
    """Lead kisi field executive ko de dijiye. staff_uid staff_list se milta hai."""
    _write_guard()
    db = _need_db()
    lead = db.collection("leads").document(lead_id)
    if not lead.get().exists:
        return {"error": "Ye lead nahi mili: " + lead_id}
    s = db.collection("staffRoles").document(staff_uid).get()
    if not s.exists or not (s.to_dict() or {}).get("active"):
        return {"error": "Ye staff nahi mila ya uska access band hai."}
    name = (s.to_dict() or {}).get("name") or ""
    lead.set({"assignedTo": staff_uid, "assignedName": name,
              "assignedAt": _now_iso(), "updatedAt": _now_iso()}, merge=True)
    _audit("mcp_lead_assign", lead_id, name)
    return {"ok": True, "id": lead_id, "assignedName": name}


@mcp.tool()
def add_staff(name: str, email: str, password: str, role: str) -> dict:
    """Naya staff banaiye — Firebase account aur role, dono ek saath.
    role: field, support ya verifier. Password kam se kam 8 akshar."""
    _write_guard()
    db = _need_db()
    if role not in ("support", "field", "verifier"):
        return {"error": "role field / support / verifier me se koi ek hona chahiye."}
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (email or "").strip()):
        return {"error": "Email theek nahi hai."}
    if len(password or "") < 8:
        return {"error": "Password kam se kam 8 akshar ka rakhiye."}
    email = email.strip().lower()
    reused = False
    try:
        user = fb_auth.create_user(email=email, password=password, display_name=name)
    except Exception:
        try:
            user = fb_auth.get_user_by_email(email)
            reused = True
        except Exception as e2:
            return {"error": "Account nahi bana: " + e2.__class__.__name__}
    if db.collection("admin").document(user.uid).get().exists:
        return {"error": "Ye account admin hai. Pehle admin se hataiye."}
    db.collection("staffRoles").document(user.uid).set({
        "role": role, "active": True, "name": (name or "").strip()[:80],
        "email": email, "updatedAt": _now_iso()}, merge=True)
    _audit("mcp_staff_create", user.uid, role)
    return {"ok": True, "uid": user.uid, "email": email, "role": role, "reused": reused,
            "message": ("Account pehle se tha — usi ko role de diya, password purana hi hai."
                        if reused else "Account ban gaya aur access de diya gaya.")}


@mcp.tool()
def disable_staff(uid: str) -> dict:
    """Staff ka access band kijiye. Record mit-ta nahi, sirf active=false hota hai."""
    _write_guard()
    db = _need_db()
    ref = db.collection("staffRoles").document(uid)
    if not ref.get().exists:
        return {"error": "Ye staff nahi mila: " + uid}
    ref.set({"active": False, "updatedAt": _now_iso()}, merge=True)
    _audit("mcp_staff_disable", uid, "")
    return {"ok": True, "uid": uid, "active": False}


# --------------------------------------------------- photos and their folders

_PHOTO_KINDS = {
    # kind -> (storage prefix, keyed by slug or by document id)
    "room":       ("business_rooms", "slug"),
    "cover":      ("covers", "slug"),
    "map":        ("main_maps", "slug"),
    "panorama":   ("panorama360", "slug"),
    "walkthrough": ("walkthroughs", "docid"),
}


@mcp.tool()
def photo_upload_link(business_id: str, kind: str = "room",
                      filename: str = "photo.jpg", content_type: str = "image/jpeg",
                      minutes: int = 30) -> dict:
    """Photo chadhane ka ek seedha link banaiye — koi bhi phone se khol kar bhej sakta hai.

    Folder khud ban jata hai: Storage me 'folder' asal me sirf naam ka hissa hai, isliye
    photo chadhte hi wo apne aap ban jata hai — alag se banane ki zarurat nahi.

    kind: room, cover, map, panorama, walkthrough.
    Link minutes ke baad mar jata hai. Chadhane ke baad attach_photo() chalaiye, tabhi
    wo photo listing me lagegi."""
    _write_guard()
    if bucket is None:
        return {"error": "Storage juda nahi hai. FIREBASE_STORAGE_BUCKET set kijiye."}
    if kind not in _PHOTO_KINDS:
        return {"error": "kind in me se ek: " + ", ".join(_PHOTO_KINDS)}
    db = _need_db()
    d = db.collection("businesses").document(business_id).get()
    if not d.exists:
        return {"error": "Ye listing nahi mili: " + business_id}
    prefix, keyed = _PHOTO_KINDS[kind]
    folder = business_id if keyed == "docid" else _slugify((d.to_dict() or {}).get("businessName") or "")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "photo.jpg")[:60]
    path = f"{prefix}/{folder}/{int(_dt.datetime.now().timestamp() * 1000)}_{safe}"
    blob = bucket.blob(path)
    url = blob.generate_signed_url(
        version="v4", method="PUT",
        expiration=_dt.timedelta(minutes=max(5, min(minutes, 120))),
        content_type=content_type)
    # Ye link ek khula darwaza hai jab tak zinda hai — kisi bhi file ko is jagah
    # chadhaya ja sakta hai. Isliye link banana bhi utna hi likhne wala kaam hai
    # jitna photo lagana, aur uska nishaan rehna chahiye.
    _audit("photo_upload_link", business_id,
           "kind=%s path=%s minutes=%d" % (kind, path, max(5, min(minutes, 120))))
    return {"ok": True, "storage_path": path, "upload_url": url,
            "content_type": content_type,
            "expires_in_minutes": max(5, min(minutes, 120)),
            "how": ("Is link par PUT kariye, header me Content-Type wahi rakhiye. "
                    "Phir attach_photo() me yahi storage_path dijiye.")}


@mcp.tool()
def attach_photo(business_id: str, storage_path: str, kind: str = "room",
                 room_id: str | None = None, slot: int = 0) -> dict:
    """Chadhi hui photo ko listing par laga dijiye.
    kind=cover to wo mukhya photo banegi; kind=room ke liye room_id aur slot (0,1,2) dijiye."""
    _write_guard()
    if bucket is None:
        return {"error": "Storage juda nahi hai."}
    db = _need_db()
    ref = db.collection("businesses").document(business_id)
    d = ref.get()
    if not d.exists:
        return {"error": "Ye listing nahi mili: " + business_id}
    blob = bucket.blob(storage_path)
    if not blob.exists():
        return {"error": "Is raste par koi file nahi hai: " + storage_path}
    # Firebase ka download URL uske apne token se banta hai. Wahi token yahan bhi
    # lagate hain, taaki ye photo bilkul waise hi khule jaise app se chadhayi hui.
    token = str(uuid.uuid4())
    meta = dict(blob.metadata or {})
    meta["firebaseStorageDownloadTokens"] = token
    blob.metadata = meta
    blob.patch()
    url = (f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/"
           f"{quote(storage_path, safe='')}?alt=media&token={token}")

    b = d.to_dict() or {}
    if kind == "cover":
        ref.set({"coverPhotoUrl": url, "lastEditedAt": _now_iso()}, merge=True)
        where = "cover photo"
    elif kind == "room":
        rooms = list(b.get("roomsAndBeds") or [])
        hit = None
        for r in rooms:
            if isinstance(r, dict) and str(r.get("id")) == str(room_id):
                hit = r
                break
        if hit is None:
            return {"error": "Ye kamra nahi mila: " + str(room_id),
                    "rooms": [r.get("id") for r in rooms if isinstance(r, dict)]}
        photos = list(hit.get("photos") or ["", "", ""])
        while len(photos) < 3:
            photos.append("")
        photos[max(0, min(slot, 2))] = url
        hit["photos"] = photos
        ref.set({"roomsAndBeds": rooms, "lastEditedAt": _now_iso()}, merge=True)
        where = f"room {hit.get('name') or room_id} slot {slot}"
    else:
        return {"error": "attach_photo abhi sirf kind=cover aur kind=room ke liye hai."}
    _audit("mcp_photo_attach", business_id, where)
    return {"ok": True, "url": url, "attached_to": where}


@mcp.tool()
def list_photos(business_id: str) -> dict:
    """Is listing ke folder me kaun kaun si photo padi hain — aur kis kamre me photo kam hai."""
    db = _need_db()
    d = db.collection("businesses").document(business_id).get()
    if not d.exists:
        return {"error": "Ye listing nahi mili: " + business_id}
    b = d.to_dict() or {}
    slug = _slugify(b.get("businessName") or "")
    files: dict[str, list[str]] = {}
    if bucket is not None:
        for prefix, keyed in _PHOTO_KINDS.values():
            folder = business_id if keyed == "docid" else slug
            got = []
            try:
                for blob in bucket.list_blobs(prefix=f"{prefix}/{folder}/", max_results=50):
                    got.append(blob.name)
            except Exception as e:
                got = ["(nahi padh paye: %s)" % e.__class__.__name__]
            if got:
                files[prefix] = got
    missing = [r.get("name") or r.get("id") for r in (b.get("roomsAndBeds") or [])
               if isinstance(r, dict) and not [p for p in (r.get("photos") or []) if p]]
    return {"businessName": b.get("businessName"), "slug": slug,
            "folders": files, "rooms_without_photos": missing,
            "hasCover": bool(b.get("coverPhotoUrl"))}


# ============================================================================
#  5.  HTTP app + the IP lock
# ============================================================================

_NETS = []
if ALLOW_CIDRS_RAW != "*":
    for part in ALLOW_CIDRS_RAW.split(","):
        part = part.strip()
        if part:
            try:
                _NETS.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                print("MCP_ALLOW_CIDRS me ye galat hai, chhoda ja raha hai:", part)


def _client_ip(scope) -> str:
    for k, v in scope.get("headers") or []:
        if k == b"x-forwarded-for":
            return v.decode().split(",")[0].strip()
    client = scope.get("client") or ("", 0)
    return client[0] or ""


class IpLock:
    """Render har request ke aage X-Forwarded-For lagata hai. Agar wo Anthropic ke
    range me nahi hai, to hum kuch bhi nahi batate — seedha 404, jaise yahan kuch ho
    hi na. Isse koi yeh bhi nahi jaan paata ki server maujood hai."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _NETS:
            return await self.app(scope, receive, send)
        ip = _client_ip(scope)
        ok = False
        try:
            addr = ipaddress.ip_address(ip)
            ok = any(addr in n for n in _NETS)
        except ValueError:
            ok = False
        if not ok:
            body = b'{"error":"not found"}'
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


app = IpLock(mcp.streamable_http_app())

if __name__ == "__main__":
    import uvicorn
    print("MCP ka rasta:", MCP_PATH)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
