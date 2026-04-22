"""
<<<<<<< HEAD
ProctorVault - Multi-role Exam Proctoring System
=======
ProctorVault v3 - Multi-role Exam Proctoring System
>>>>>>> 7af4a12 (reset and push local)
Face Identity: ArcFace / Glintr100 ONNX  (drop glintr100.onnx next to this file)
Roles: Admin | Teacher | Student  |  Run: python server.py
"""
import os, sys, cv2, mediapipe as mp, numpy as np, threading, sqlite3, time, base64
import json, uuid, secrets
from collections import deque
from datetime import datetime, timedelta
from ultralytics import YOLO
import onnxruntime as ort
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, session, send_from_directory, make_response)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

# ─── Config ────────────────────────────────────────────────────────────────────
EAR_BLINK_THRESHOLD   = 0.25
EAR_CONSEC_FRAMES     = 3
AUTO_LOCK_THRESHOLD   = 82
AUTO_LOCK_HOLD        = 3.0
SAVE_SNAPSHOT         = True
SNAPSHOT_DIR          = "snapshots"
UPLOAD_DIR            = "uploads"
DB_PATH               = "proctovault.db"
SECRET_KEY            = secrets.token_hex(32)
FRAME_QUALITY         = 65
THUMB_SIZE            = (320, 180)
SUSPICIOUS_LABELS     = {"cell phone","mobile phone","phone","book","cellphone",
                         "notebook","radio","laptop"}
FACE_VERIFY_INTERVAL  = 30        # seconds between live ID checks during exam
FACE_MATCH_THRESHOLD  = 0.50      # ArcFace cosine similarity threshold (>0.5 = same person)
FACE_FAIL_LIMIT       = 3
ALLOWED_IMG_EXT       = {'.jpg','.jpeg','.png','.webp','.gif'}
ARCFACE_IMG_SIZE      = 112       # ArcFace / Glintr100 input size
ARCFACE_MODEL_PATH    = "glintr100.onnx"

for d in [SNAPSHOT_DIR, UPLOAD_DIR, "uploads/id_photos", "uploads/qimages",
          "uploads/enrolled", "static",
          "templates/admin", "templates/teacher", "templates/student"]:
    os.makedirs(d, exist_ok=True)

# ─── Flask / SocketIO ──────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    max_http_buffer_size=16*1024*1024, ping_timeout=60)

# ─── Database ──────────────────────────────────────────────────────────────────
db_lock = threading.Lock()

def get_db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    conn = get_db(); c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL, password TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
        created_at TEXT DEFAULT (datetime('now')), active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS exams (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT,
        teacher_id INTEGER NOT NULL, exam_code TEXT UNIQUE NOT NULL,
        duration_mins INTEGER DEFAULT 60, max_attempts INTEGER DEFAULT 1,
        shuffle_q INTEGER DEFAULT 1,
        status TEXT DEFAULT 'draft' CHECK(status IN ('draft','active','closed')),
        created_at TEXT DEFAULT (datetime('now')),
        starts_at TEXT, ends_at TEXT,
        face_verify_enabled INTEGER DEFAULT 0,
        face_verify_thresh REAL DEFAULT 0.50,
        face_fail_limit INTEGER DEFAULT 3,
        FOREIGN KEY(teacher_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id TEXT NOT NULL,
        content TEXT NOT NULL, options TEXT NOT NULL,
        correct_idx INTEGER NOT NULL, points INTEGER DEFAULT 1,
        order_idx INTEGER DEFAULT 0, FOREIGN KEY(exam_id) REFERENCES exams(id)
    );
    CREATE TABLE IF NOT EXISTS exam_sessions (
        id TEXT PRIMARY KEY, exam_id TEXT NOT NULL,
        student_id INTEGER, student_name TEXT NOT NULL, student_email TEXT NOT NULL,
        socket_id TEXT, started_at TEXT, ended_at TEXT, submitted_at TEXT,
        score REAL, max_score REAL, answers TEXT DEFAULT '{}',
        current_q INTEGER DEFAULT 0, time_remaining INTEGER,
        locked INTEGER DEFAULT 0, lock_reason TEXT DEFAULT '',
        paused INTEGER DEFAULT 0, attention_avg REAL DEFAULT 70,
        suspicion_max REAL DEFAULT 0, flags INTEGER DEFAULT 0,
        status TEXT DEFAULT 'waiting'
            CHECK(status IN ('waiting','id_pending','active','paused','locked','submitted')),
        id_photo TEXT DEFAULT '', id_embedding TEXT DEFAULT '',
        id_verified INTEGER DEFAULT 0, id_fail_count INTEGER DEFAULT 0,
        FOREIGN KEY(exam_id) REFERENCES exams(id)
    );
    CREATE TABLE IF NOT EXISTS enrolled_students (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id     TEXT NOT NULL,
        name        TEXT NOT NULL,
        email       TEXT NOT NULL,
        photo       TEXT DEFAULT '',
        embedding   TEXT DEFAULT '',
        enrolled_at TEXT DEFAULT (datetime('now')),
        enrolled_by INTEGER,
        UNIQUE(exam_id, email),
        FOREIGN KEY(exam_id) REFERENCES exams(id)
    );
    CREATE TABLE IF NOT EXISTS proctor_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        exam_id TEXT NOT NULL, timestamp TEXT DEFAULT (datetime('now')),
        attention REAL, suspicion REAL, event_type TEXT, details TEXT, snapshot TEXT,
        FOREIGN KEY(session_id) REFERENCES exam_sessions(id)
    );
    CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        user_id INTEGER, action TEXT, details TEXT, ip TEXT
    );
    """)
    conn.commit()
    c.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
    if not c.fetchone():
        c.execute("INSERT INTO users (email,name,password,role) VALUES (?,?,?,?)",
                  ("admin@proctovault.com","System Admin",
                   generate_password_hash("admin123"),"admin"))
        conn.commit()
        print("  Default admin: admin@proctovault.com / admin123", flush=True)
    conn.close()

init_db()

def db_exec(sql, params=()):
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute(sql, params); conn.commit()
        lid = c.lastrowid; conn.close(); return lid

def db_query(sql, params=()):
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute(sql, params); rows = c.fetchall()
        conn.close(); return [dict(r) for r in rows]

def db_one(sql, params=()):
    r = db_query(sql, params); return r[0] if r else None

def log_action(user_id, action, details=""):
    try:
        db_exec("INSERT INTO system_logs (user_id,action,details,ip) VALUES (?,?,?,?)",
                (user_id, action, details, request.remote_addr if request else "system"))
    except: pass

# ─── Auth ──────────────────────────────────────────────────────────────────────
def login_required(roles=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'user_id' not in session: return redirect(url_for('login_page'))
            if roles and session.get('role') not in roles: return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ─── Vision Models ─────────────────────────────────────────────────────────────
print("Loading YOLO...", flush=True)
try:
    yolo_model = YOLO("yolov8n.pt"); YOLO_OK = True; print("YOLO loaded.", flush=True)
except Exception as e:
    YOLO_OK = False; print(f"YOLO unavailable: {e}", flush=True)

# MediaPipe — still used for proctoring (eye/gaze/hand/face mesh) but NOT for embeddings
mp_face_mesh_mod = mp.solutions.face_mesh
mp_hands_mod     = mp.solutions.hands
mp_drawing       = mp.solutions.drawing_utils

# ─── MediaPipe face detection (for ArcFace crop + live ID check) ──────────────
# One shared detector instance, protected by a single lock.
# IMPORTANT: MediaPipe.process() is NOT thread-safe — the lock MUST wrap the
# entire detector creation AND the process() call together.  We use a single
# non-reentrant lock and never call _crop_face from within a function that
# already holds this lock.
_mp_face_det      = None
_mp_face_det_lock = threading.Lock()   # guards both init and process()

def _init_mp_face_detector():
    """Must be called while already holding _mp_face_det_lock."""
    global _mp_face_det
    if _mp_face_det is None:
        _mp_face_det = mp.solutions.face_detection.FaceDetection(
            model_selection=0, min_detection_confidence=0.6)

def make_face_mesh():
    return mp_face_mesh_mod.FaceMesh(static_image_mode=False, max_num_faces=1,
        refine_landmarks=True, min_detection_confidence=0.5, min_tracking_confidence=0.5)

def make_hands():
    return mp_hands_mod.Hands(static_image_mode=False, max_num_hands=2,
        min_detection_confidence=0.5, min_tracking_confidence=0.5)

# ─── ArcFace / Glintr100 ONNX — CPU-only setup ────────────────────────────────
# ort.InferenceSession.run() IS thread-safe on CPU provider, so no extra lock
# is needed around sess.run() calls; only session creation is guarded.
_arcface_session  = None
_arcface_lock     = threading.Lock()

def get_arcface():
    """Lazy-load the ONNX session (CPU only). Thread-safe init, fast path after."""
    global _arcface_session
    if _arcface_session is not None:
        return _arcface_session
    with _arcface_lock:
        if _arcface_session is not None:   # re-check after acquiring
            return _arcface_session
        if not os.path.exists(ARCFACE_MODEL_PATH):
            _log_cmd(f"[ArcFace] ⚠  '{ARCFACE_MODEL_PATH}' not found — face verify disabled")
            return None
        try:
            _log_cmd("[ArcFace] Loading model (CPU)…")
            _arcface_session = ort.InferenceSession(
                ARCFACE_MODEL_PATH,
                providers=["CPUExecutionProvider"])   # CPU-only, no CUDA
            _log_cmd(f"[ArcFace] ✅ Model ready — input: {_arcface_session.get_inputs()[0].name}")
        except Exception as e:
            _log_cmd(f"[ArcFace] ❌ Failed to load: {e}")
            return None
    return _arcface_session

# Maximum dimension we'll feed to the face detector / ArcFace pipeline.
# Anything larger gets downscaled first — keeps CPU times fast.
_MAX_PROC_DIM = 480   # pixels — large enough for good detection, small enough for speed

def _resize_for_processing(img_bgr: np.ndarray) -> np.ndarray:
    """Downscale img_bgr so the longest side is at most _MAX_PROC_DIM."""
    h, w = img_bgr.shape[:2]
    longest = max(h, w)
    if longest <= _MAX_PROC_DIM:
        return img_bgr
    scale = _MAX_PROC_DIM / longest
    return cv2.resize(img_bgr, (int(w * scale), int(h * scale)),
                      interpolation=cv2.INTER_AREA)

def _arcface_preprocess(face_bgr: np.ndarray) -> np.ndarray:
    """Resize to 112×112, BGR→RGB, normalise to [-1,1], return NCHW float32."""
    face = cv2.resize(face_bgr, (ARCFACE_IMG_SIZE, ARCFACE_IMG_SIZE),
                      interpolation=cv2.INTER_LINEAR)
    face = cv2.cvtColor(face, cv2.COLOR_BGR2RGB).astype(np.float32)
    face = (face - 127.5) / 127.5
    face = np.transpose(face, (2, 0, 1))   # HWC → CHW
    return np.expand_dims(face, axis=0)    # → (1, 3, 112, 112)

def _crop_face_from(img_bgr: np.ndarray):
    """
    Detect and crop the largest face in img_bgr using MediaPipe FaceDetection.
    img_bgr should already be downscaled to _MAX_PROC_DIM before calling this.
    Returns (face_crop_bgr, annotated_img_bgr) or (None, None).
    Thread-safe: acquires _mp_face_det_lock for the entire detect call.
    """
    h, w = img_bgr.shape[:2]
    rgb  = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

    with _mp_face_det_lock:
        _init_mp_face_detector()               # no-op if already initialised
        result = _mp_face_det.process(rgb)     # single lock covers both init & process

    if not result.detections:
        return None, None

    det = result.detections[0]
    box = det.location_data.relative_bounding_box
    x1  = max(0, int(box.xmin * w))
    y1  = max(0, int(box.ymin * h))
    x2  = min(w, int((box.xmin + box.width)  * w))
    y2  = min(h, int((box.ymin + box.height) * h))

    # 10 % margin so forehead and chin are included
    mx = max(4, int((x2 - x1) * 0.10))
    my = max(4, int((y2 - y1) * 0.10))
    x1 = max(0, x1 - mx); y1 = max(0, y1 - my)
    x2 = min(w, x2 + mx); y2 = min(h, y2 + my)

    face_crop = img_bgr[y1:y2, x1:x2]
    if face_crop.size == 0:
        return None, None

    dbg = img_bgr.copy()
    cv2.rectangle(dbg, (x1, y1), (x2, y2), (0, 220, 80), 2)
    cv2.putText(dbg, "FACE DETECTED", (x1, max(y1 - 8, 0)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 80), 2)
    return face_crop, dbg

def _log_cmd(msg: str):
    """Print to stdout immediately (flush) so CMD shows it in real time."""
    print(msg, flush=True)

def extract_face_embedding(img_bgr: np.ndarray):
    """
    Full ArcFace embedding pipeline with CMD progress output.
    Steps:
      [10%] Check model available
      [25%] Downscale image for CPU speed
      [40%] Run MediaPipe face detection
      [60%] Crop + preprocess face to 112×112
      [80%] ONNX inference → 512-d vector
      [100%] L2 normalise, encode debug image

    Returns (embedding_list, debug_b64_jpeg) or (None, None).
    """
    _log_cmd("[ID]  10% — checking ArcFace model…")

    sess = get_arcface()
    if sess is None:
        _log_cmd("[ID]  ❌ ArcFace model not available")
        return None, None

    _log_cmd("[ID]  25% — downscaling image for CPU…")
    small = _resize_for_processing(img_bgr)
    orig_h, orig_w = img_bgr.shape[:2]
    sm_h,   sm_w   = small.shape[:2]
    _log_cmd(f"[ID]       original {orig_w}×{orig_h}  →  working {sm_w}×{sm_h}")

    _log_cmd("[ID]  40% — detecting face…")
    face_crop, dbg_small = _crop_face_from(small)
    if face_crop is None:
        _log_cmd("[ID]  ❌ No face detected — photo rejected")
        return None, None
    _log_cmd(f"[ID]       face crop {face_crop.shape[1]}×{face_crop.shape[0]} px")

    _log_cmd("[ID]  60% — preprocessing face for ArcFace (112×112)…")
    inp     = _arcface_preprocess(face_crop)

    _log_cmd("[ID]  80% — running ONNX inference on CPU…")
    t0      = time.time()
    in_name = sess.get_inputs()[0].name
    emb     = sess.run(None, {in_name: inp})[0].flatten()
    ms      = int((time.time() - t0) * 1000)
    _log_cmd(f"[ID]       inference done in {ms} ms")

    _log_cmd("[ID] 100% — normalising embedding & encoding debug image…")
    norm = np.linalg.norm(emb)
    if norm > 0:
        emb = emb / norm
    emb_list = emb.tolist()

    # Encode annotated debug image (downscaled to keep response small)
    _, buf = cv2.imencode('.jpg', dbg_small, [cv2.IMWRITE_JPEG_QUALITY, 72])
    b64 = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()

    _log_cmd(f"[ID]  ✅ Embedding ready  dim={len(emb_list)}  norm≈1.0")
    return emb_list, b64

def cosine_sim(a, b) -> float:
    """Cosine similarity. Returns float in [0.0, 1.0] for normalised embeddings."""
    a = np.array(a, dtype=np.float32)
    b = np.array(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))

# ─── Per-session proctor state ─────────────────────────────────────────────────
proctor_sessions = {}; ps_lock = threading.Lock()

class PSession:
    def __init__(self, sid, session_id, exam_id, student_name,
                 face_verify=False, face_thresh=FACE_MATCH_THRESHOLD,
                 face_fail_limit=FACE_FAIL_LIMIT, stored_emb=None):
        self.sid=sid; self.session_id=session_id
        self.exam_id=exam_id; self.student_name=student_name
        self.attention=70.0; self.suspicion=30.0
        self.locked=False; self.lock_reason=""; self.paused=False
        self.last_frame=None; self.audio_history=deque(maxlen=80)
        self.eye_closed_t=None; self.sus_hold_t=None
        self.leq=deque(maxlen=EAR_CONSEC_FRAMES); self.req=deque(maxlen=EAR_CONSEC_FRAMES)
        self.flags=0; self.face_mesh=make_face_mesh(); self.hands_mp=make_hands()
        self.face_verify=face_verify; self.face_thresh=face_thresh
        self.face_fail_limit=face_fail_limit; self.stored_emb=stored_emb
        self.id_fail_count=0; self.last_id_t=time.time()-FACE_VERIFY_INTERVAL
        self.id_score=None

    def cleanup(self):
        try: self.face_mesh.close()
        except: pass
        try: self.hands_mp.close()
        except: pass

    def to_dict(self):
        return {"sid":self.sid,"session_id":self.session_id,"exam_id":self.exam_id,
                "name":self.student_name,"attention":round(self.attention,1),
                "suspicion":round(self.suspicion,1),"locked":self.locked,
                "lock_reason":self.lock_reason,"paused":self.paused,"flags":self.flags,
                "id_score":round(self.id_score,2) if self.id_score is not None else None,
                "face_verify":self.face_verify}

# ─── Geometry helpers (for proctoring, NOT identity) ─────────────────────────
def euclidean(a,b): return float(np.linalg.norm(np.array(a)-np.array(b)))

def ear(pts):
    A=euclidean(pts[1],pts[5]); B=euclidean(pts[2],pts[4]); C=euclidean(pts[0],pts[3])
    return (A+B)/(2.0*C) if C else 0.0

def iris_pos(eye_pts, iris_pts):
    xl,xr=eye_pts[0][0],eye_pts[3][0]
    ix=float(np.mean([p[0] for p in iris_pts])); iy=float(np.mean([p[1] for p in iris_pts]))
    px=(ix-xl)/max(xr-xl,1); ys=[p[1] for p in eye_pts]
    py=(iy-min(ys))/max(max(ys)-min(ys),1)
    return float(np.clip(px,0,1)),float(np.clip(py,0,1))

def allowed_image(fn): return os.path.splitext(fn.lower())[1] in ALLOWED_IMG_EXT

def save_upload(fobj, sub=""):
    folder = os.path.join(UPLOAD_DIR,sub) if sub else UPLOAD_DIR
    os.makedirs(folder,exist_ok=True)
    ext = os.path.splitext(secure_filename(fobj.filename))[1].lower()
    name = uuid.uuid4().hex+ext
    fobj.save(os.path.join(folder,name))
    return name

# ─── Live identity check inside process_frame ─────────────────────────────────
def _live_identity_check(ps: PSession, frame: np.ndarray, yn: int, h: int) -> tuple:
    """
    Run ArcFace identity check on the current proctoring frame.
    - Downscales to _MAX_PROC_DIM so CPU stays responsive
    - Uses _crop_face_from (correct lock, no deadlock)
    - ort.InferenceSession.run() is thread-safe on CPUExecutionProvider
    Returns (id_result_dict | None, id_fail_bool).
    """
    id_result = None; id_fail = False
    try:
        sess = get_arcface()
        if sess is None:
            return None, False

        # Downscale the live frame before face detection — saves ~3-4x CPU time
        small     = _resize_for_processing(frame)
        face_crop, _ = _crop_face_from(small)
        if face_crop is None:
            _log_cmd(f"[LiveID] no face in frame for {ps.student_name[:16]}")
            return None, False

        inp      = _arcface_preprocess(face_crop)
        in_name  = sess.get_inputs()[0].name
        live_emb = sess.run(None, {in_name: inp})[0].flatten()
        norm     = np.linalg.norm(live_emb)
        if norm > 0:
            live_emb /= norm

        sim = cosine_sim(ps.stored_emb, live_emb.tolist())
        ps.id_score = sim
        label_y = yn - 70 if yn > 80 else 90

        if sim < ps.face_thresh:
            ps.id_fail_count += 1
            db_exec("UPDATE exam_sessions SET id_fail_count=? WHERE id=?",
                    (ps.id_fail_count, ps.session_id))
            id_result = {"match": False, "sim": round(sim, 3), "fails": ps.id_fail_count}
            id_fail   = ps.id_fail_count >= ps.face_fail_limit
            cv2.putText(frame, f"ID MISMATCH ({sim:.0%})", (10, label_y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 230), 2)
            _log_cmd(f"[LiveID] ❌ {ps.student_name[:16]}  sim={sim:.3f}  fails={ps.id_fail_count}/{ps.face_fail_limit}")
        else:
            ps.id_fail_count = max(0, ps.id_fail_count - 1)
            db_exec("UPDATE exam_sessions SET id_fail_count=? WHERE id=?",
                    (ps.id_fail_count, ps.session_id))
            id_result = {"match": True, "sim": round(sim, 3), "fails": ps.id_fail_count}
            cv2.putText(frame, f"ID OK ({sim:.0%})", (10, label_y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 80), 2)
            _log_cmd(f"[LiveID] ✅ {ps.student_name[:16]}  sim={sim:.3f}")
    except Exception as e:
        _log_cmd(f"[LiveID] error: {e}")
    return id_result, id_fail

# ─── Frame processing ──────────────────────────────────────────────────────────
def process_frame(ps: PSession, frame: np.ndarray, audio_rms: float) -> dict:
    h,w=frame.shape[:2]; rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
    detected=[]; sus_objects=[]
    if YOLO_OK:
        try:
            for r in yolo_model(frame,verbose=False):
                for box in r.boxes:
                    cls=int(box.cls.cpu().numpy()[0]); lbl=yolo_model.model.names[cls].lower()
                    conf=float(box.conf.cpu().numpy()[0]); xyxy=box.xyxy[0].cpu().numpy()
                    detected.append(lbl)
                    x1,y1,x2,y2=map(int,xyxy)
                    col=(0,60,220) if lbl in SUSPICIOUS_LABELS else (50,180,50)
                    cv2.rectangle(frame,(x1,y1),(x2,y2),col,2)
                    cv2.putText(frame,f"{lbl} {conf:.2f}",(x1,max(y1-4,0)),cv2.FONT_HERSHEY_SIMPLEX,0.4,col,1)
                    if lbl in SUSPICIOUS_LABELS and conf>0.35: sus_objects.append(lbl)
        except: pass
    people=detected.count("person")

    hr=ps.hands_mp.process(rgb); hand_pts=[]
    if hr.multi_hand_landmarks:
        for hl in hr.multi_hand_landmarks:
            mp_drawing.draw_landmarks(frame,hl,mp_hands_mod.HAND_CONNECTIONS,
                mp_drawing.DrawingSpec(color=(0,200,50),thickness=1,circle_radius=2),
                mp_drawing.DrawingSpec(color=(80,240,80),thickness=1))
            hand_pts.append([(int(l.x*w),int(l.y*h)) for l in hl.landmark])

    fr=ps.face_mesh.process(rgb)
    attentive=looking_away=face_covered=eye_covered=eyes_closed=talking=False
    gaze_dir="Center"; events=[]; yn=80; yx=h-80  # fallback positions for labels

    if fr.multi_face_landmarks:
        fl=fr.multi_face_landmarks[0]
        pts={i:(int(l.x*w),int(l.y*h)) for i,l in enumerate(fl.landmark)}
        xs=[p[0] for p in pts.values()]; ys=[p[1] for p in pts.values()]
        xn,xx,yn,yx=min(xs),max(xs),min(ys),max(ys); fh=yx-yn

        mp_drawing.draw_landmarks(frame,fl,mp_face_mesh_mod.FACEMESH_TESSELATION,
            mp_drawing.DrawingSpec(color=(0,180,0),thickness=1,circle_radius=1),
            mp_drawing.DrawingSpec(color=(60,120,220),thickness=1))

        LEI=[33,159,158,133,145,153]; REI=[362,386,387,263,373,374]
        lep=[pts[i] for i in LEI]; rep=[pts[i] for i in REI]
        le=ear(lep); re=ear(rep); ps.leq.append(le); ps.req.append(re)
        al=float(np.mean(ps.leq)); ar=float(np.mean(ps.req))
        cv2.putText(frame,f"L:{al:.2f} R:{ar:.2f}",(8,54),cv2.FONT_HERSHEY_SIMPLEX,0.45,(230,230,0),1)

        eyes_now=al<EAR_BLINK_THRESHOLD and ar<EAR_BLINK_THRESHOLD
        if eyes_now:
            if ps.eye_closed_t is None: ps.eye_closed_t=time.time()
            elif time.time()-ps.eye_closed_t>0.5: eyes_closed=True
        else: eyes_closed=False; ps.eye_closed_t=None

        ul,ll=pts.get(13,(0,0)),pts.get(14,(0,0))
        mr=euclidean(ul,ll)/max(fh,1); talking=audio_rms>0.018 and mr>0.04

        LI=[468,469,470,471]; RI=[473,474,475,476]
        lip=[pts[i] for i in LI if i in pts]; rip=[pts[i] for i in RI if i in pts]
        if lip and rip:
            lx,ly=iris_pos(lep,lip); rx,ry=iris_pos(rep,rip)
            ax,ay=(lx+rx)/2,(ly+ry)/2
            if ay>0.66: gaze_dir="Down"; looking_away=True
            elif ax<0.34: gaze_dir="Left"; looking_away=True
            elif ax>0.66: gaze_dir="Right"; looking_away=True
            cv2.putText(frame,f"Gaze:{gaze_dir}",(xn,yx+20),cv2.FONT_HERSHEY_SIMPLEX,0.5,(220,220,220),1)
            for p in lip+rip: cv2.circle(frame,p,2,(220,0,220),-1)

        for hp in hand_pts:
            for hx,hy in hp:
                for ep in [lep,rep]:
                    if min(p[0] for p in ep)<=hx<=max(p[0] for p in ep) and \
                       min(p[1] for p in ep)<=hy<=max(p[1] for p in ep): eye_covered=True
        for hp in hand_pts:
            inside=sum(1 for (hx,hy) in hp if xn<=hx<=xx and yn<=hy<=yx)
            chin_y=pts.get(152,(0,yx))[1]
            near_c=sum(1 for (hx,hy) in hp if abs(hy-chin_y)<40 and xn<=hx<=xx)
            if inside>=4 and near_c<4: face_covered=True

        attentive=not(looking_away or face_covered or eye_covered or eyes_closed)
        if eyes_closed:  cv2.putText(frame,"EYES CLOSED",(xn,yn-52),cv2.FONT_HERSHEY_SIMPLEX,0.65,(0,0,255),2); events.append("eyes_closed")
        if eye_covered:  cv2.putText(frame,"EYES COVERED",(xn,yn-36),cv2.FONT_HERSHEY_SIMPLEX,0.65,(0,0,255),2); events.append("eye_covered")
        if face_covered: cv2.putText(frame,"FACE COVERED",(xn,yn-20),cv2.FONT_HERSHEY_SIMPLEX,0.65,(0,0,255),2); events.append("face_covered")
        if looking_away: events.append(f"gaze_{gaze_dir.lower()}")
        if talking:      cv2.putText(frame,"TALKING",(xn,yn-4),cv2.FONT_HERSHEY_SIMPLEX,0.6,(0,80,255),2); events.append("talking")
    else:
        cv2.putText(frame,"NO FACE DETECTED",(20,80),cv2.FONT_HERSHEY_SIMPLEX,0.75,(0,0,255),2)
        events.append("no_face")

    if sus_objects:  events.extend([f"object:{o}" for o in sus_objects])
    if people>1:     events.append(f"multiple_people:{people}")

    # ── ArcFace identity check (periodic, only when face verify is on) ────────
    id_result=None; id_fail=False
    if (ps.face_verify and ps.stored_emb
            and (time.time()-ps.last_id_t) >= FACE_VERIFY_INTERVAL):
        ps.last_id_t = time.time()
        id_result, id_fail = _live_identity_check(ps, frame, yn, h)
        if id_result:
            if not id_result["match"]:
                events.append(f"identity_mismatch:sim={id_result['sim']:.2f}")
            # else no event needed — clean pass is silent

    # ── Scoring ───────────────────────────────────────────────────────────────
    ds=0; da=0
    if people>1:       ds+=35
    if sus_objects:    ds+=30
    if face_covered:   ds+=25
    if eye_covered:    ds+=20; da-=15
    if eyes_closed:    ds+=18; da-=28
    elif looking_away: ds+=14; da-=18
    if talking:        ds+=18
    if attentive:      da+=15; ds-=10
    if id_result and not id_result.get("match",True): ds+=25
    ps.attention=float(np.clip(ps.attention+da*0.1,0,100))
    ps.suspicion=float(np.clip(ps.suspicion+ds*0.1,0,100))

    ps.audio_history.append(audio_rms)
    _draw_audio(frame,w,h,list(ps.audio_history),audio_rms)
    cv2.rectangle(frame,(0,0),(w,46),(12,12,18),-1)
    ac=(0,200,60) if ps.attention>60 else (0,120,255) if ps.attention>35 else (0,40,220)
    sc=(0,200,60) if ps.suspicion<40 else (0,120,255) if ps.suspicion<70 else (0,40,220)
    cv2.putText(frame,f"ATT:{ps.attention:.0f}%",(8,30),cv2.FONT_HERSHEY_DUPLEX,0.75,ac,2)
    cv2.putText(frame,f"SUS:{ps.suspicion:.0f}%",(200,30),cv2.FONT_HERSHEY_DUPLEX,0.75,sc,2)
    if ps.face_verify and ps.id_score is not None:
        ic=(0,200,60) if ps.id_score>ps.face_thresh else (0,40,220)
        cv2.putText(frame,f"ID:{ps.id_score:.0%}",(385,30),cv2.FONT_HERSHEY_SIMPLEX,0.65,ic,2)
    cv2.putText(frame,ps.student_name[:18],(w-190,30),cv2.FONT_HERSHEY_SIMPLEX,0.5,(160,160,160),1)

    now=time.time(); triggered=False; trigger_reason=""
    if ps.suspicion>=AUTO_LOCK_THRESHOLD:
        if ps.sus_hold_t is None: ps.sus_hold_t=now
        elif (now-ps.sus_hold_t)>=AUTO_LOCK_HOLD and not ps.locked:
            triggered=True; ps.flags+=1; trigger_reason=f"Auto-lock: suspicion {ps.suspicion:.0f}%"
    else: ps.sus_hold_t=None
    if id_fail and not ps.locked:
        triggered=True; ps.flags+=1
        trigger_reason=f"Identity mismatch: {ps.id_fail_count} consecutive failures"
    if ps.locked:
        ov=frame.copy(); cv2.rectangle(ov,(0,int(h*.3)),(w,int(h*.7)),(0,0,160),-1)
        cv2.addWeighted(ov,0.72,frame,0.28,0,frame)
        cv2.putText(frame,"SESSION LOCKED",(int(w*.08),int(h*.52)),cv2.FONT_HERSHEY_DUPLEX,1.6,(255,255,255),4)
    return {"events":events,"triggered":triggered,"trigger_reason":trigger_reason,
            "sus_objects":sus_objects,"people":people,"id_result":id_result}

def _draw_audio(frame,w,h,hist,current):
    gh=60; gy0=h-gh
    cv2.rectangle(frame,(0,gy0),(w,h),(18,18,24),-1)
    if len(hist)>1:
        vals=np.clip(np.array(hist)/0.06,0,1)
        xs=np.linspace(0,w,len(vals)).astype(int); ys=(h-(vals*gh*0.9)).astype(int)
        col=(0,220,80) if current<0.018 else (0,80,220)
        for i in range(1,len(xs)): cv2.line(frame,(xs[i-1],ys[i-1]),(xs[i],ys[i]),col,1)
    cv2.putText(frame,"AUDIO",(6,gy0-3),cv2.FONT_HERSHEY_SIMPLEX,0.38,(100,200,100),1)
    rw=int(min(current/0.06,1)*80)
    cv2.rectangle(frame,(w-90,gy0+8),(w-90+rw,gy0+20),(0,200,80),-1)

# ─── SocketIO ──────────────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect(): pass

@socketio.on("disconnect")
def on_disconnect():
    sid=request.sid
    with ps_lock: ps=proctor_sessions.pop(sid,None)
    if ps:
        db_exec("UPDATE exam_sessions SET socket_id=NULL,ended_at=datetime('now') WHERE id=?",(ps.session_id,))
        ps.cleanup()
        socketio.emit("student_left",{"sid":sid,"session_id":ps.session_id},room=f"exam_{ps.exam_id}")
        socketio.emit("student_left",{"sid":sid,"session_id":ps.session_id},room="admins")

@socketio.on("join_exam_proctor")
def on_join_exam(data):
    sid=request.sid; session_id=data.get("session_id")
    es=db_one("SELECT es.*,e.face_verify_enabled,e.face_verify_thresh,e.face_fail_limit "
              "FROM exam_sessions es JOIN exams e ON es.exam_id=e.id WHERE es.id=?",(session_id,))
    if not es: emit("error",{"msg":"Invalid session"}); return
    emb=json.loads(es["id_embedding"]) if es.get("id_embedding") else None
    ps=PSession(sid,session_id,es["exam_id"],es["student_name"],
                face_verify=bool(es["face_verify_enabled"]),
                face_thresh=float(es.get("face_verify_thresh") or FACE_MATCH_THRESHOLD),
                face_fail_limit=int(es.get("face_fail_limit") or FACE_FAIL_LIMIT),
                stored_emb=emb)
    ps.locked=bool(es["locked"]); ps.paused=bool(es["paused"])
    ps.id_fail_count=int(es.get("id_fail_count") or 0)
    with ps_lock: proctor_sessions[sid]=ps
    db_exec("UPDATE exam_sessions SET socket_id=?,status='active',started_at=COALESCE(started_at,datetime('now')) WHERE id=?",(sid,session_id))
    join_room(f"exam_{es['exam_id']}")
    emit("proctor_ready",{"locked":ps.locked,"paused":ps.paused,
                          "face_verify":ps.face_verify,"id_verified":bool(es.get("id_verified"))})
    socketio.emit("student_joined",ps.to_dict(),room=f"exam_{es['exam_id']}")
    socketio.emit("student_joined",ps.to_dict(),room="admins")

@socketio.on("join_admin_room")
def on_join_admin():
    join_room("admins")
    with ps_lock: emit("all_students",[ps.to_dict() for ps in proctor_sessions.values()])

@socketio.on("join_teacher_room")
def on_join_teacher(data):
    eid=data.get("exam_id"); join_room(f"exam_{eid}")
    with ps_lock: emit("exam_students",[ps.to_dict() for ps in proctor_sessions.values() if ps.exam_id==eid])

@socketio.on("video_frame")
def on_frame(data):
    sid=request.sid
    with ps_lock: ps=proctor_sessions.get(sid)
    if not ps: return
    try:
        b64=data["frame"].split(",")[-1]; audio=float(data.get("audio_rms",0))
        buf=np.frombuffer(base64.b64decode(b64),dtype=np.uint8)
        frame=cv2.imdecode(buf,cv2.IMREAD_COLOR)
        if frame is None: return
        frame=cv2.resize(frame,(640,int(frame.shape[0]*640/max(frame.shape[1],1))))
        result=process_frame(ps,frame,audio)
        _,jpg=cv2.imencode(".jpg",frame,[cv2.IMWRITE_JPEG_QUALITY,FRAME_QUALITY])
        b64out="data:image/jpeg;base64,"+base64.b64encode(jpg.tobytes()).decode()
        _,th=cv2.imencode(".jpg",cv2.resize(frame,THUMB_SIZE),[cv2.IMWRITE_JPEG_QUALITY,50])
        b64th="data:image/jpeg;base64,"+base64.b64encode(th.tobytes()).decode()
        ps.last_frame=b64th
        emit("processed_frame",{"frame":b64out,"attention":ps.attention,"suspicion":ps.suspicion,
                                 "events":result["events"],"id_result":result["id_result"]})
        socketio.emit("student_update",{**ps.to_dict(),"thumb":b64th},room=f"exam_{ps.exam_id}")
        socketio.emit("student_update",{**ps.to_dict(),"thumb":b64th},room="admins")
        notable=[e for e in result["events"] if "center" not in e]
        if notable:
            snap=None
            if SAVE_SNAPSHOT and (result["triggered"] or len(notable)>2):
                fname=secure_filename(f"snap_{ps.session_id}_{int(time.time())}.jpg")
                cv2.imwrite(os.path.join(SNAPSHOT_DIR,fname),frame); snap=fname
            db_exec("INSERT INTO proctor_events (session_id,exam_id,attention,suspicion,event_type,details,snapshot) VALUES (?,?,?,?,?,?,?)",
                    (ps.session_id,ps.exam_id,round(ps.attention,1),round(ps.suspicion,1),
                     notable[0],json.dumps(notable),snap or ""))
            db_exec("UPDATE exam_sessions SET attention_avg=(attention_avg*0.95+?*0.05),suspicion_max=MAX(suspicion_max,?),flags=flags+? WHERE id=?",
                    (ps.attention,ps.suspicion,1 if result["triggered"] else 0,ps.session_id))
        if result["triggered"] and not ps.locked:
            _lock_session(ps,result["trigger_reason"] or "Auto-lock triggered")
    except Exception as ex: print(f"[frame err] {ex}")

@socketio.on("admin_lock_session")
def s_lock(data): _find_and_lock(data.get("session_id"),data.get("reason","Admin locked"),True); emit("action_ack",{"ok":True})
@socketio.on("admin_unlock_session")
def s_unlock(data): _find_and_lock(data.get("session_id"),"",False); emit("action_ack",{"ok":True})
@socketio.on("admin_lock_exam")
def e_lock(data):
    eid=data.get("exam_id"); reason=data.get("reason","Locked by teacher")
    with ps_lock: sids=[p.sid for p in proctor_sessions.values() if p.exam_id==eid]
    for sid in sids:
        with ps_lock: p=proctor_sessions.get(sid)
        if p: _lock_session(p,reason)
@socketio.on("admin_unlock_exam")
def e_unlock(data):
    eid=data.get("exam_id")
    with ps_lock: sids=[p.sid for p in proctor_sessions.values() if p.exam_id==eid]
    for sid in sids:
        with ps_lock: p=proctor_sessions.get(sid)
        if p: _unlock_session(p)
@socketio.on("admin_broadcast_lock")
def b_lock(data):
    reason=data.get("reason","Admin locked all")
    with ps_lock: sids=list(proctor_sessions.keys())
    for sid in sids:
        with ps_lock: p=proctor_sessions.get(sid)
        if p and not p.locked: _lock_session(p,reason)
@socketio.on("admin_broadcast_unlock")
def b_unlock(_):
    with ps_lock: sids=list(proctor_sessions.keys())
    for sid in sids:
        with ps_lock: p=proctor_sessions.get(sid)
        if p: _unlock_session(p)
@socketio.on("submit_answer")
def s_answer(data):
    sid=request.sid
    with ps_lock: ps=proctor_sessions.get(sid)
    if not ps or ps.locked: return
    es=db_one("SELECT answers FROM exam_sessions WHERE id=?",(ps.session_id,))
    if not es: return
    ans=json.loads(es["answers"] or "{}"); ans[str(data.get("q_id"))]=data.get("answer")
    db_exec("UPDATE exam_sessions SET answers=?,current_q=? WHERE id=?",(json.dumps(ans),data.get("q_idx",0),ps.session_id))
@socketio.on("submit_exam")
def s_submit(data):
    sid=request.sid
    with ps_lock: ps=proctor_sessions.get(sid)
    if not ps: return
    _grade_and_submit(ps.session_id); emit("exam_submitted",{"message":"Submitted."})

def _find_and_lock(session_id,reason,lock):
    with ps_lock: ps=next((p for p in proctor_sessions.values() if p.session_id==session_id),None)
    if ps:
        if lock: _lock_session(ps,reason)
        else:    _unlock_session(ps)
    db_exec("UPDATE exam_sessions SET locked=?,lock_reason=?,paused=? WHERE id=?",
            (1 if lock else 0,reason,1 if lock else 0,session_id))

def _lock_session(ps,reason):
    ps.locked=True; ps.paused=True; ps.lock_reason=reason
    db_exec("UPDATE exam_sessions SET locked=1,paused=1,lock_reason=?,status='locked' WHERE id=?",(reason,ps.session_id))
    socketio.emit("session_locked",{"reason":reason,"locked":True,"paused":True},room=ps.sid)
    socketio.emit("student_update",ps.to_dict(),room=f"exam_{ps.exam_id}")
    socketio.emit("student_update",ps.to_dict(),room="admins")
    db_exec("INSERT INTO proctor_events (session_id,exam_id,event_type,details) VALUES (?,?,?,?)",
            (ps.session_id,ps.exam_id,"session_locked",reason))

def _unlock_session(ps):
    ps.locked=False; ps.paused=False; ps.lock_reason=""
    db_exec("UPDATE exam_sessions SET locked=0,paused=0,lock_reason='',status='active' WHERE id=?",(ps.session_id,))
    socketio.emit("session_unlocked",{"locked":False,"paused":False},room=ps.sid)
    socketio.emit("student_update",ps.to_dict(),room=f"exam_{ps.exam_id}")
    socketio.emit("student_update",ps.to_dict(),room="admins")

def _grade_and_submit(session_id):
    es=db_one("SELECT * FROM exam_sessions WHERE id=?",(session_id,))
    if not es or es["submitted_at"]: return
    questions=db_query("SELECT * FROM questions WHERE exam_id=? ORDER BY order_idx",(es["exam_id"],))
    answers=json.loads(es["answers"] or "{}"); score=0; max_score=sum(q["points"] for q in questions)
    for q in questions:
        ans=answers.get(str(q["id"]))
        if ans is not None and int(ans)==q["correct_idx"]: score+=q["points"]
    db_exec("UPDATE exam_sessions SET submitted_at=datetime('now'),score=?,max_score=?,status='submitted' WHERE id=?",
            (score,max_score,session_id))

# ─── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index(): return redirect(url_for('dashboard') if 'user_id' in session else url_for('login_page'))

@app.route("/login",methods=["GET","POST"])
def login_page():
    error=None
    if request.method=="POST":
        email=request.form.get("email","").strip().lower(); pwd=request.form.get("password","")
        user=db_one("SELECT * FROM users WHERE email=? AND active=1",(email,))
        if user and check_password_hash(user["password"],pwd):
            session.permanent=True
            session.update({"user_id":user["id"],"name":user["name"],"email":user["email"],"role":user["role"]})
            log_action(user["id"],"login"); return redirect(url_for("dashboard"))
        error="Invalid credentials"
    return render_template("login.html",error=error)

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login_page"))

@app.route("/dashboard")
@login_required()
def dashboard():
    role=session.get("role")
    if role=="admin": return redirect(url_for("admin_dashboard"))
    if role=="teacher": return redirect(url_for("teacher_dashboard"))
    return redirect(url_for("student_dashboard"))

@app.route("/student")
@login_required(["student"])
def student_dashboard(): return render_template("student/dashboard.html",user=session)

# ── Student public ─────────────────────────────────────────────────────────────
@app.route("/join",methods=["GET","POST"])
def join_exam():
    error=None
    if request.method=="POST":
        code =request.form.get("code","").strip().upper()
        name =request.form.get("name","").strip()
        email=request.form.get("email","").strip().lower()
        exam =db_one("SELECT * FROM exams WHERE exam_code=? AND status='active'",(code,))
        if not exam: error="Exam not found or not active."
        elif exam["ends_at"] and datetime.utcnow().isoformat()>exam["ends_at"]:
            error="This exam has ended."
        else:
            sid=str(uuid.uuid4())
            needs_verify = bool(exam["face_verify_enabled"])
            enrolled = None
            if needs_verify:
                enrolled = db_one(
                    "SELECT * FROM enrolled_students WHERE exam_id=? AND email=?",
                    (exam["id"], email))
                if not enrolled:
                    enrolled = db_one(
                        "SELECT * FROM enrolled_students WHERE exam_id=? AND lower(name)=?",
                        (exam["id"], name.lower()))
                if not enrolled or not enrolled["embedding"]:
                    error = ("No enrolled record found for this email/name in this exam. "
                             "Contact your teacher to register your photo before the exam.")
                    return render_template("student/join.html", error=error)

            status   = "id_pending" if needs_verify else "waiting"
            emb_json = enrolled["embedding"] if enrolled else ""
            db_exec(
                "INSERT INTO exam_sessions "
                "(id,exam_id,student_name,student_email,time_remaining,status,id_embedding,id_verified) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sid, exam["id"], name, email, exam["duration_mins"]*60, status, emb_json, 0))
            log_action(0,"student_join",f"name={name} exam={exam['id']}")
            if needs_verify:
                return redirect(url_for("student_identity", session_id=sid))
            return redirect(url_for("student_syscheck", session_id=sid))
    return render_template("student/join.html", error=error)


@app.route("/identity/<session_id>", methods=["GET","POST"])
def student_identity(session_id):
    """
    Live face verification against PRE-STORED teacher reference photo.
    Student captures face via webcam → ArcFace embedding → compared to enrolled embedding.
    Student CANNOT upload an arbitrary photo.
    """
    es = db_one(
        "SELECT es.*,e.title,e.face_verify_thresh,e.face_fail_limit "
        "FROM exam_sessions es JOIN exams e ON es.exam_id=e.id WHERE es.id=?",
        (session_id,))
    if not es: return redirect(url_for("join_exam"))
    if es["id_verified"]: return redirect(url_for("student_syscheck", session_id=session_id))

    enrolled = db_one(
        "SELECT * FROM enrolled_students WHERE exam_id=? AND lower(email)=?",
        (es["exam_id"], es["student_email"].lower()))
    if not enrolled:
        enrolled = db_one(
            "SELECT * FROM enrolled_students WHERE exam_id=? AND lower(name)=?",
            (es["exam_id"], es["student_name"].lower()))

    ref_photo_url = None
    if enrolled and enrolled["photo"]:
        ref_photo_url = f"/uploads/enrolled/{enrolled['photo']}"

    result = None; error = None

    if request.method == "POST":
        photo_b64 = request.form.get("photo_b64","")
        img_bgr   = None
        if photo_b64:
            try:
                buf     = np.frombuffer(base64.b64decode(photo_b64.split(",")[-1]),dtype=np.uint8)
                img_bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            except:
                error = "Could not read captured photo."

        if img_bgr is None and not error:
            error = "Please capture your photo using the camera."

        if img_bgr is not None and not error:
            live_emb, debug_b64 = extract_face_embedding(img_bgr)
            if live_emb is None:
                error = "No face detected. Look directly at the camera in good lighting."
            else:
                stored_emb_json = es.get("id_embedding","")
                if not stored_emb_json:
                    error = "No reference embedding found. Contact your teacher."
                else:
                    stored_emb = json.loads(stored_emb_json)
                    sim        = cosine_sim(stored_emb, live_emb)
                    thresh     = float(es.get("face_verify_thresh") or FACE_MATCH_THRESHOLD)
                    matched    = sim >= thresh
                    result     = {"match": matched, "sim": round(sim,3), "threshold": thresh}

                    cap_fname = f"cap_{session_id}_{int(time.time())}.jpg"
                    cv2.imwrite(os.path.join(UPLOAD_DIR,"id_photos",cap_fname), img_bgr)

                    if matched:
                        db_exec(
                            "UPDATE exam_sessions SET id_photo=?,id_verified=1,status='waiting' WHERE id=?",
                            (cap_fname, session_id))
                        db_exec(
                            "INSERT INTO proctor_events (session_id,exam_id,event_type,details) "
                            "VALUES (?,?,?,?)",
                            (session_id, es["exam_id"], "identity_verified",
                             f"sim={sim:.3f} threshold={thresh:.2f}"))
                        log_action(0,"id_verified",f"session={session_id} sim={sim:.3f}")
                        return redirect(url_for("student_syscheck", session_id=session_id))
                    else:
                        fail_count = int(es.get("id_fail_count",0)) + 1
                        db_exec("UPDATE exam_sessions SET id_fail_count=? WHERE id=?",(fail_count,session_id))
                        db_exec(
                            "INSERT INTO proctor_events (session_id,exam_id,event_type,details) "
                            "VALUES (?,?,?,?)",
                            (session_id, es["exam_id"], "identity_mismatch_at_entry",
                             f"sim={sim:.3f} threshold={thresh:.2f} fail_count={fail_count}"))
                        fail_limit = int(es.get("face_fail_limit") or FACE_FAIL_LIMIT)
                        if fail_count >= fail_limit * 2:
                            db_exec("UPDATE exam_sessions SET status='locked',locked=1,lock_reason=? WHERE id=?",
                                    ("Identity verification failed too many times at entry.", session_id))
                            error = "Too many failed attempts. Your session has been locked. Contact your teacher."
                        else:
                            error = (f"Identity could not be verified ({sim:.0%} match, "
                                     f"need {thresh:.0%}). Try better lighting.")

    return render_template(
        "student/identity.html",
        session=es, error=error, result=result, ref_photo_url=ref_photo_url)

@app.route("/syscheck/<session_id>")
def student_syscheck(session_id):
    es=db_one("SELECT es.*,e.title,e.face_verify_enabled,e.face_verify_thresh,e.duration_mins "
              "FROM exam_sessions es JOIN exams e ON es.exam_id=e.id WHERE es.id=?",(session_id,))
    if not es: return redirect(url_for("join_exam"))
    return render_template("student/syscheck.html",session=es)

@app.route("/exam/<session_id>")
def student_exam(session_id):
    es=db_one("SELECT es.*,e.* FROM exam_sessions es JOIN exams e ON es.exam_id=e.id WHERE es.id=?",(session_id,))
    if not es or es["status"]=="submitted": return redirect(url_for("exam_result",session_id=session_id))
    questions=db_query("SELECT * FROM questions WHERE exam_id=? ORDER BY order_idx",(es["exam_id"],))
    answers=json.loads(es["answers"] or "{}")
    for q in questions:
        try: q["content_parsed"]=json.loads(q["content"])
        except: q["content_parsed"]={"blocks":[{"type":"text","value":q.get("content","")}]}
        try: opts=json.loads(q["options"])
        except: opts=[]
        q["options_parsed"]=[o if isinstance(o,dict) else {"type":"text","value":str(o)} for o in opts]
    return render_template("student/exam.html",session=es,questions=questions,answers=answers)

@app.route("/result/<session_id>")
def exam_result(session_id):
    es=db_one("SELECT es.*,e.title FROM exam_sessions es JOIN exams e ON es.exam_id=e.id WHERE es.id=?",(session_id,))
    if not es: return "Not found",404
    return render_template("student/result.html",session=es)

# ── Teacher ────────────────────────────────────────────────────────────────────
@app.route("/teacher")
@login_required(["teacher","admin"])
def teacher_dashboard():
    uid=session["user_id"]
    exams=db_query("SELECT e.*,COUNT(es.id) as session_count FROM exams e LEFT JOIN exam_sessions es ON e.id=es.exam_id WHERE e.teacher_id=? GROUP BY e.id ORDER BY e.created_at DESC",(uid,))
    return render_template("teacher/dashboard.html",exams=exams,user=session)

@app.route("/teacher/exam/new",methods=["GET","POST"])
@login_required(["teacher","admin"])
def new_exam():
    if request.method=="POST":
        d=request.form; eid=str(uuid.uuid4()); code=secrets.token_urlsafe(5).upper()
        db_exec("INSERT INTO exams (id,title,description,teacher_id,exam_code,duration_mins,max_attempts,shuffle_q,status,starts_at,ends_at,face_verify_enabled,face_verify_thresh,face_fail_limit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid,d["title"],d.get("description",""),session["user_id"],code,
                 int(d.get("duration",60)),int(d.get("max_attempts",1)),1 if d.get("shuffle") else 0,
                 d.get("status","draft"),d.get("starts_at") or None,d.get("ends_at") or None,
                 1 if d.get("face_verify") else 0,float(d.get("face_thresh",0.50)),int(d.get("face_fail_limit",3))))
        log_action(session["user_id"],"create_exam",eid)
        return redirect(url_for("edit_exam",exam_id=eid))
    return render_template("teacher/new_exam.html",user=session)

@app.route("/teacher/exam/<exam_id>/edit",methods=["GET","POST"])
@login_required(["teacher","admin"])
def edit_exam(exam_id):
    exam=db_one("SELECT * FROM exams WHERE id=?",(exam_id,))
    if not exam: return "Not found",404
    if request.method=="POST":
        d=request.form
        db_exec("UPDATE exams SET title=?,description=?,duration_mins=?,status=?,starts_at=?,ends_at=?,face_verify_enabled=?,face_verify_thresh=?,face_fail_limit=? WHERE id=?",
                (d["title"],d.get("description",""),int(d.get("duration",60)),d.get("status","draft"),
                 d.get("starts_at") or None,d.get("ends_at") or None,
                 1 if d.get("face_verify") else 0,float(d.get("face_thresh",0.50)),
                 int(d.get("face_fail_limit",3)),exam_id))
        return redirect(url_for("edit_exam",exam_id=exam_id))
    questions=db_query("SELECT * FROM questions WHERE exam_id=? ORDER BY order_idx",(exam_id,))
    return render_template("teacher/edit_exam.html",exam=exam,questions=questions,user=session)


# ── Roster management (teacher) ────────────────────────────────────────────────
@app.route("/teacher/exam/<exam_id>/roster")
@login_required(["teacher","admin"])
def exam_roster(exam_id):
    exam     = db_one("SELECT * FROM exams WHERE id=?",(exam_id,))
    if not exam: return "Not found",404
    students = db_query("SELECT * FROM enrolled_students WHERE exam_id=? ORDER BY name",(exam_id,))
    return render_template("teacher/roster.html", exam=exam, students=students, user=session)

@app.route("/teacher/exam/<exam_id>/roster/add", methods=["POST"])
@login_required(["teacher","admin"])
def roster_add_student(exam_id):
    name  = request.form.get("name","").strip()
    email = request.form.get("email","").strip().lower()
    if not name or not email:
        return jsonify({"ok":False,"error":"Name and email required"}), 400

    photo_file = request.files.get("photo")
    photo_fname = ""; emb_json = ""

    if photo_file and photo_file.filename and allowed_image(photo_file.filename):
        buf     = np.frombuffer(photo_file.read(), dtype=np.uint8)
        img_bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img_bgr is not None:
            emb, _ = extract_face_embedding(img_bgr)
            if emb:
                emb_json    = json.dumps(emb)
                photo_fname = f"{uuid.uuid4().hex}.jpg"
                os.makedirs(os.path.join(UPLOAD_DIR,"enrolled"), exist_ok=True)
                cv2.imwrite(os.path.join(UPLOAD_DIR,"enrolled",photo_fname), img_bgr)

    existing = db_one("SELECT id FROM enrolled_students WHERE exam_id=? AND email=?",(exam_id,email))
    if existing:
        if emb_json:
            db_exec("UPDATE enrolled_students SET name=?,photo=?,embedding=?,enrolled_by=? WHERE exam_id=? AND email=?",
                    (name,photo_fname,emb_json,session["user_id"],exam_id,email))
        else:
            db_exec("UPDATE enrolled_students SET name=?,enrolled_by=? WHERE exam_id=? AND email=?",
                    (name,session["user_id"],exam_id,email))
    else:
        db_exec("INSERT INTO enrolled_students (exam_id,name,email,photo,embedding,enrolled_by) VALUES (?,?,?,?,?,?)",
                (exam_id,name,email,photo_fname,emb_json,session["user_id"]))

    log_action(session["user_id"],"roster_add",f"exam={exam_id} email={email} has_photo={bool(emb_json)}")
    return redirect(url_for("exam_roster",exam_id=exam_id))


@app.route("/teacher/exam/<exam_id>/roster/upload_photo/<int:student_id>", methods=["POST"])
@login_required(["teacher","admin"])
def roster_upload_photo(exam_id, student_id):
    photo_file = request.files.get("photo")
    if not photo_file or not photo_file.filename or not allowed_image(photo_file.filename):
        return jsonify({"ok":False,"error":"No valid image"}), 400

    buf     = np.frombuffer(photo_file.read(), dtype=np.uint8)
    img_bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img_bgr is None:
        return jsonify({"ok":False,"error":"Cannot decode image"}), 400

    emb, debug_b64 = extract_face_embedding(img_bgr)
    if not emb:
        return jsonify({"ok":False,"error":"No face detected. Use a clear frontal image."}), 400

    fname = f"{uuid.uuid4().hex}.jpg"
    os.makedirs(os.path.join(UPLOAD_DIR,"enrolled"), exist_ok=True)
    cv2.imwrite(os.path.join(UPLOAD_DIR,"enrolled",fname), img_bgr)

    db_exec("UPDATE enrolled_students SET photo=?,embedding=? WHERE id=? AND exam_id=?",
            (fname, json.dumps(emb), student_id, exam_id))
    log_action(session["user_id"],"roster_photo_update",f"student_id={student_id}")
    return jsonify({"ok":True,"debug_img":debug_b64,"photo_url":f"/uploads/enrolled/{fname}"})


@app.route("/teacher/exam/<exam_id>/roster/delete/<int:student_id>", methods=["POST"])
@login_required(["teacher","admin"])
def roster_delete_student(exam_id, student_id):
    db_exec("DELETE FROM enrolled_students WHERE id=? AND exam_id=?",(student_id,exam_id))
    return redirect(url_for("exam_roster",exam_id=exam_id))


@app.route("/teacher/exam/<exam_id>/roster/csv", methods=["POST"])
@login_required(["teacher","admin"])
def roster_import_csv(exam_id):
    f = request.files.get("csv_file")
    if not f: return redirect(url_for("exam_roster",exam_id=exam_id))
    lines = f.read().decode("utf-8","ignore").splitlines()
    added=0
    for line in lines[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2: continue
        name,email = parts[0],parts[1].lower()
        if not name or not email or "@" not in email: continue
        existing = db_one("SELECT id FROM enrolled_students WHERE exam_id=? AND email=?",(exam_id,email))
        if not existing:
            db_exec("INSERT INTO enrolled_students (exam_id,name,email,enrolled_by) VALUES (?,?,?,?)",
                    (exam_id,name,email,session["user_id"]))
            added+=1
    log_action(session["user_id"],"csv_import",f"exam={exam_id} added={added}")
    return redirect(url_for("exam_roster",exam_id=exam_id)+"?imported="+str(added))


@app.route("/api/enrolled/<int:student_id>/verify_preview", methods=["POST"])
@login_required(["teacher","admin"])
def api_enrolled_verify_preview(student_id):
    data = request.get_json()
    frame_b64 = data.get("frame","").split(",")[-1]
    enrolled  = db_one("SELECT * FROM enrolled_students WHERE id=?",(student_id,))
    if not enrolled or not enrolled["embedding"]:
        return jsonify({"ok":False,"error":"No embedding for this student"})
    try:
        buf   = np.frombuffer(base64.b64decode(frame_b64),dtype=np.uint8)
        frame = cv2.imdecode(buf,cv2.IMREAD_COLOR)
        emb,_ = extract_face_embedding(frame)
        if emb is None: return jsonify({"ok":True,"match":False,"sim":0,"reason":"No face"})
        stored = json.loads(enrolled["embedding"])
        sim    = cosine_sim(stored,emb)
        return jsonify({"ok":True,"match":sim>=FACE_MATCH_THRESHOLD,"sim":round(sim,3)})
    except Exception as ex:
        return jsonify({"ok":False,"error":str(ex)})

@app.route("/teacher/exam/<exam_id>/monitor")
@login_required(["teacher","admin"])
def monitor_exam(exam_id):
    exam=db_one("SELECT * FROM exams WHERE id=?",(exam_id,))
    if not exam: return "Not found",404
    sessions=db_query("SELECT * FROM exam_sessions WHERE exam_id=? AND status NOT IN ('submitted') ORDER BY started_at DESC",(exam_id,))
    return render_template("teacher/monitor.html",exam=exam,sessions=sessions,user=session)

@app.route("/teacher/exam/<exam_id>/report")
@login_required(["teacher","admin"])
def exam_report(exam_id):
    exam=db_one("SELECT e.*,u.name as teacher_name FROM exams e JOIN users u ON e.teacher_id=u.id WHERE e.id=?",(exam_id,))
    sessions=db_query("SELECT * FROM exam_sessions WHERE exam_id=? ORDER BY started_at DESC",(exam_id,))
    events=db_query("SELECT pe.*,es.student_name FROM proctor_events pe JOIN exam_sessions es ON pe.session_id=es.id WHERE pe.exam_id=? ORDER BY pe.timestamp DESC",(exam_id,))
    questions=db_query("SELECT * FROM questions WHERE exam_id=? ORDER BY order_idx",(exam_id,))
    return render_template("teacher/report.html",exam=exam,sessions=sessions,events=events,questions=questions,user=session)

# ── Admin ──────────────────────────────────────────────────────────────────────
@app.route("/admin")
@login_required(["admin"])
def admin_dashboard():
    stats={
        "users":    db_one("SELECT COUNT(*) as c FROM users")["c"],
        "exams":    db_one("SELECT COUNT(*) as c FROM exams")["c"],
        "active_exams": db_one("SELECT COUNT(*) as c FROM exams WHERE status='active'")["c"],
        "sessions": db_one("SELECT COUNT(*) as c FROM exam_sessions")["c"],
        "events":   db_one("SELECT COUNT(*) as c FROM proctor_events")["c"],
        "active":   db_one("SELECT COUNT(*) as c FROM exam_sessions WHERE status='active'")["c"],
        "enrolled": db_one("SELECT COUNT(*) as c FROM enrolled_students")["c"],
        "enrolled_with_photo": db_one("SELECT COUNT(*) as c FROM enrolled_students WHERE embedding!='' AND embedding IS NOT NULL")["c"],
    }
    recent_events=db_query(
        "SELECT pe.*,es.student_name,es.exam_id FROM proctor_events pe "
        "JOIN exam_sessions es ON pe.session_id=es.id ORDER BY pe.timestamp DESC LIMIT 20")
    live_exams=db_query(
        "SELECT e.*,COUNT(es.id) as active_count FROM exams e "
        "JOIN exam_sessions es ON e.id=es.exam_id WHERE es.status='active' GROUP BY e.id")
    return render_template("admin/dashboard.html",stats=stats,
                           recent_events=recent_events,live_exams=live_exams,user=session)

@app.route("/admin/users")
@login_required(["admin"])
def admin_users():
    return render_template("admin/users.html",users=db_query("SELECT * FROM users ORDER BY created_at DESC"),user=session)

@app.route("/admin/users/create",methods=["POST"])
@login_required(["admin"])
def admin_create_user():
    d=request.form
    db_exec("INSERT INTO users (email,name,password,role) VALUES (?,?,?,?)",
            (d["email"].lower(),d["name"],generate_password_hash(d["password"]),d["role"]))
    log_action(session["user_id"],"create_user",d["email"]); return redirect(url_for("admin_users"))

@app.route("/admin/users/<int:uid>/toggle")
@login_required(["admin"])
def admin_toggle_user(uid): db_exec("UPDATE users SET active=1-active WHERE id=?",(uid,)); return redirect(url_for("admin_users"))

@app.route("/admin/exams")
@login_required(["admin"])
def admin_exams():
    return render_template("admin/exams.html",user=session,
        exams=db_query("SELECT e.*,u.name as teacher_name,COUNT(es.id) as session_count FROM exams e LEFT JOIN users u ON e.teacher_id=u.id LEFT JOIN exam_sessions es ON e.id=es.exam_id GROUP BY e.id ORDER BY e.created_at DESC"))

@app.route("/admin/sessions")
@login_required(["admin"])
def admin_sessions():
    return render_template("admin/sessions.html",user=session,
        sessions=db_query("SELECT es.*,e.title as exam_title FROM exam_sessions es JOIN exams e ON es.exam_id=e.id ORDER BY es.started_at DESC LIMIT 500"))

@app.route("/admin/live")
@login_required(["admin"])
def admin_live():
    return render_template("admin/live.html",user=session,
        active=db_query("SELECT es.*,e.title FROM exam_sessions es JOIN exams e ON es.exam_id=e.id WHERE es.status='active' ORDER BY es.started_at DESC"),
        exams=db_query("SELECT * FROM exams WHERE status='active'"))

@app.route("/admin/logs")
@login_required(["admin"])
def admin_logs():
    return render_template("admin/logs.html",user=session,
        logs=db_query("SELECT sl.*,u.name as user_name FROM system_logs sl LEFT JOIN users u ON sl.user_id=u.id ORDER BY sl.timestamp DESC LIMIT 500"))

# ── API ────────────────────────────────────────────────────────────────────────
@app.route("/api/questions",methods=["POST"])
@login_required(["teacher","admin"])
def api_add_question():
    if request.content_type and "multipart" in request.content_type:
        exam_id     =request.form.get("exam_id","")
        content     =json.loads(request.form.get("content",'{"blocks":[]}'))
        options     =json.loads(request.form.get("options","[]"))
        correct_idx =int(request.form.get("correct_idx",0))
        points      =int(request.form.get("points",1))
        order_idx   =int(request.form.get("order_idx",0))
        qimg=request.files.get("q_img")
        if qimg and qimg.filename and allowed_image(qimg.filename):
            name=save_upload(qimg,"qimages")
            content.setdefault("blocks",[]).append({"type":"image","value":f"/uploads/qimages/{name}"})
        for i in range(len(options)):
            oimg=request.files.get(f"opt_img_{i}")
            if oimg and oimg.filename and allowed_image(oimg.filename):
                name=save_upload(oimg,"qimages")
                if isinstance(options[i],dict): options[i]["image"]=f"/uploads/qimages/{name}"
                else: options[i]={"type":"text","value":str(options[i]),"image":f"/uploads/qimages/{name}"}
    else:
        d=request.get_json()
        exam_id=d.get("exam_id",""); content=d.get("content",{"blocks":[]})
        options=d.get("options",[]); correct_idx=int(d.get("correct_idx",0))
        points=int(d.get("points",1)); order_idx=int(d.get("order_idx",0))
    oid=db_exec("INSERT INTO questions (exam_id,content,options,correct_idx,points,order_idx) VALUES (?,?,?,?,?,?)",
                (exam_id,json.dumps(content),json.dumps(options),correct_idx,points,order_idx))
    return jsonify({"ok":True,"id":oid})

@app.route("/api/questions/<int:qid>",methods=["GET"])
@login_required(["teacher","admin"])
def api_get_question(qid):
    q = db_one("SELECT * FROM questions WHERE id=?",(qid,))
    if not q: return jsonify({"ok":False,"error":"Not found"}),404
    try: q["content_parsed"] = json.loads(q["content"])
    except: q["content_parsed"] = {"blocks":[{"type":"text","value":q.get("content","")}]}
    try: q["options_parsed"]  = json.loads(q["options"])
    except: q["options_parsed"] = []
    return jsonify({"ok":True,"question":q})

@app.route("/api/questions/<int:qid>",methods=["DELETE"])
@login_required(["teacher","admin"])
def api_del_question(qid): db_exec("DELETE FROM questions WHERE id=?",(qid,)); return jsonify({"ok":True})

@app.route("/api/questions/<int:qid>",methods=["PUT"])
@login_required(["teacher","admin"])
def api_update_question(qid):
    d=request.get_json()
    db_exec("UPDATE questions SET content=?,options=?,correct_idx=?,points=? WHERE id=?",
            (json.dumps(d.get("content",{})),json.dumps(d.get("options",[])),
             int(d.get("correct_idx",0)),int(d.get("points",1)),qid))
    return jsonify({"ok":True})

@app.route("/api/upload_image",methods=["POST"])
@login_required(["teacher","admin"])
def api_upload_image():
    f=request.files.get("image")
    if not f or not f.filename or not allowed_image(f.filename):
        return jsonify({"ok":False,"error":"Invalid file"}),400
    name=save_upload(f,"qimages")
    return jsonify({"ok":True,"url":f"/uploads/qimages/{name}"})

@app.route("/api/verify_face",methods=["POST"])
def api_verify_face():
    data=request.get_json()
    session_id=data.get("session_id"); frame_b64=data.get("frame","").split(",")[-1]
    es=db_one("SELECT id_embedding,face_verify_thresh FROM exam_sessions es "
              "JOIN exams e ON es.exam_id=e.id WHERE es.id=?",(session_id,))
    if not es or not es.get("id_embedding"):
        return jsonify({"ok":False,"error":"No reference stored"})
    try:
        buf=np.frombuffer(base64.b64decode(frame_b64),dtype=np.uint8)
        frame=cv2.imdecode(buf,cv2.IMREAD_COLOR)
        emb,_=extract_face_embedding(frame)
        if emb is None: return jsonify({"ok":True,"match":False,"sim":0,"reason":"No face"})
        stored=json.loads(es["id_embedding"]); sim=cosine_sim(stored,emb)
        thresh=float(es.get("face_verify_thresh") or FACE_MATCH_THRESHOLD)
        return jsonify({"ok":True,"match":sim>=thresh,"sim":round(sim,3),"threshold":thresh})
    except Exception as ex: return jsonify({"ok":False,"error":str(ex)})

@app.route("/api/exam/<exam_id>/sessions")
@login_required(["teacher","admin"])
def api_exam_sessions(exam_id):
    return jsonify(db_query("SELECT * FROM exam_sessions WHERE exam_id=? ORDER BY started_at DESC",(exam_id,)))

@app.route("/api/exam/<exam_id>/events")
@login_required(["teacher","admin"])
def api_exam_events(exam_id):
    return jsonify(db_query("SELECT pe.*,es.student_name FROM proctor_events pe JOIN exam_sessions es ON pe.session_id=es.id WHERE pe.exam_id=? ORDER BY pe.timestamp DESC LIMIT 300",(exam_id,)))

@app.route("/api/report/<exam_id>/download")
@login_required(["teacher","admin"])
def download_report(exam_id):
    exam=db_one("SELECT e.*,u.name as teacher_name FROM exams e JOIN users u ON e.teacher_id=u.id WHERE e.id=?",(exam_id,))
    sessions=db_query("SELECT * FROM exam_sessions WHERE exam_id=? ORDER BY started_at",(exam_id,))
    events=db_query("SELECT pe.*,es.student_name FROM proctor_events pe JOIN exam_sessions es ON pe.session_id=es.id WHERE pe.exam_id=? ORDER BY pe.timestamp",(exam_id,))
    lines=[f"EXAM REPORT: {exam['title']}",f"Teacher: {exam['teacher_name']}",
           f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
           f"Exam Code: {exam['exam_code']}",
           f"Face Verification: {'ENABLED (ArcFace, thresh='+str(exam['face_verify_thresh'])+')' if exam['face_verify_enabled'] else 'DISABLED'}",
           "="*70,f"\nTOTAL PARTICIPANTS: {len(sessions)}",
           f"SUBMITTED: {len([s for s in sessions if s['submitted_at']])}",
           f"FLAGGED: {len([s for s in sessions if s['flags']>0])}"]
    submitted=[s for s in sessions if s['submitted_at']]
    if submitted:
        avg=sum(s['score'] or 0 for s in submitted)/len(submitted)
        lines.append(f"AVERAGE SCORE: {avg:.1f}")
    lines.append("\n"+"="*70+"\nSTUDENT RESULTS\n"+"="*70)
    for s in sessions:
        lines.append(f"\nName:    {s['student_name']}")
        lines.append(f"Email:   {s['student_email']}")
        sc=f"{s['score']:.0f}/{s['max_score']:.0f}" if s['score'] is not None else "Not submitted"
        lines.append(f"Score:   {sc}")
        lines.append(f"Flags:   {s['flags']}  |  ID Fails: {s['id_fail_count']}")
        lines.append(f"Status:  {s['status']}  |  Locked: {'YES - '+s['lock_reason'] if s['locked'] else 'No'}")
        lines.append(f"Attn:    {s['attention_avg']:.1f}%  |  Max Sus: {s['suspicion_max']:.1f}%")
        stu_ev=[e for e in events if e['session_id']==s['id']]
        if stu_ev:
            lines.append(f"  Events ({len(stu_ev)}):")
            for ev in stu_ev[:10]: lines.append(f"    [{ev['timestamp']}] {ev['event_type']} | {ev['details']}")
    lines.append("\n"+"="*70+"\nALL PROCTOR EVENTS\n"+"="*70)
    for ev in events[:200]:
        lines.append(f"[{ev['timestamp']}] {ev['student_name']:20s} | {ev['event_type']:20s} | ATT:{ev['attention']:.0f}% SUS:{ev['suspicion']:.0f}%")
    resp=make_response("\n".join(lines))
    resp.headers["Content-Disposition"]=f"attachment; filename=report_{exam['exam_code']}_{datetime.utcnow().strftime('%Y%m%d')}.txt"
    resp.headers["Content-Type"]="text/plain"
    return resp

@app.route("/snapshot/<path:fn>")
@login_required(["teacher","admin"])
def serve_snapshot(fn): return send_from_directory(SNAPSHOT_DIR,fn)

@app.route("/uploads/<path:fn>")
def serve_upload(fn): return send_from_directory(UPLOAD_DIR,fn)

# ─── Entry ─────────────────────────────────────────────────────────────────────
if __name__=="__main__":
    print("\n╔════════════════════════════════════════╗")
    print("║       ProctorVault  v3.1 — ArcFace     ║")
    print("╠════════════════════════════════════════╣")
    print("║  Model file : glintr100.onnx           ║")
    print("║  Admin/Teacher  →  /login              ║")
    print("║  Student        →  /join               ║")
    print("╚════════════════════════════════════════╝\n")
    # Eagerly warm up ArcFace so the first student request isn't slow
    print("\n[Startup] Warming up ArcFace model…", flush=True)
    _sess = get_arcface()
    if _sess:
        print("[Startup] ✅ ArcFace warm-up complete — ready to verify identities", flush=True)
    else:
        print("[Startup] ⚠  ArcFace not loaded — place glintr100.onnx next to server.py", flush=True)
    print("", flush=True)
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
