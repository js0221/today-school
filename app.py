"""오늘의 학교 v2 · Python 3.10+ · 외부 패키지 불필요."""
import argparse
import os
import getpass
import hashlib
import hmac
import html
import json
import re
import secrets
import sqlite3
import time
import threading
from datetime import date, datetime, timedelta, timezone
import comcigan
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('SCHOOL_DATA_DIR', str(ROOT))).resolve()
DB_PATH = DATA_DIR / 'school_v2.db'
PUBLIC_ORIGIN = os.environ.get('PUBLIC_ORIGIN', os.environ.get('RENDER_EXTERNAL_URL', '')).rstrip('/')
if PUBLIC_ORIGIN:
    public_url = urlsplit(PUBLIC_ORIGIN)
    if (public_url.scheme != 'https' or not public_url.hostname or public_url.username
            or public_url.password or public_url.path or public_url.query or public_url.fragment):
        raise RuntimeError('PUBLIC_ORIGIN must be an HTTPS origin without a path')
CONFIG_PATH = ROOT / 'config.json'
VERSION = '2.7 · 접근 보안 강화'
DEFAULT_PORT = 5001
ALLERGENS = dict(enumerate(['난류', '우유', '메밀', '땅콩', '대두', '밀', '고등어', '게', '새우',
    '돼지고기', '복숭아', '토마토', '아황산류', '호두', '닭고기', '쇠고기', '오징어', '조개류', '잣'], 1))
ALIASES = {name: number for number, name in ALLERGENS.items()}
ALIASES.update({'계란': 1, '달걀': 1, '콩': 5, '소고기': 16, '조개': 18, '굴': 18, '전복': 18, '홍합': 18})


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def fail(status, message):
    raise ApiError(status, message)


def connect():
    database_url = os.environ.get('DATABASE_URL', '').strip()
    if database_url:
        from postgres_backend import PostgresConnection
        return PostgresConnection(database_url)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('student','admin')),
            status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
            allergies TEXT NOT NULL DEFAULT '', exams TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, csrf TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id), expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY, actor INTEGER NOT NULL,
            action TEXT NOT NULL, target INTEGER, created REAL NOT NULL
        );
        ''')
        db.execute("UPDATE sessions SET expires=MIN(expires,?) WHERE user_id IN (SELECT id FROM users WHERE role='admin')", (time.time() + 1800,))


def initialize_hosted_settings():
    if not PUBLIC_ORIGIN:
        return
    if not os.environ.get('DATABASE_URL', '').strip():
        raise RuntimeError('Set DATABASE_URL before deploying; temporary SQLite storage is disabled')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            login = os.environ.get('INITIAL_ADMIN_ID', '').strip()
            password = os.environ.get('INITIAL_ADMIN_PASSWORD', '')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{2,29}', login) or not 4 <= len(password) <= 10:
                raise RuntimeError('Set INITIAL_ADMIN_ID and INITIAL_ADMIN_PASSWORD in hosting secrets')
            db.execute("INSERT INTO users(login,name,password,role,status,created) VALUES (?,'관리자',?,'admin','approved',?)",
                       (login, password_hash(password), time.time()))
        school_code = os.environ.get('COMCIGAN_SCHOOL_CODE', '')
        if school_code and not setting(db, 'comcigan_school', {}):
            code = int(school_code)
            if not 1 <= code <= 99999999:
                raise RuntimeError('Invalid COMCIGAN_SCHOOL_CODE')
            set_setting(db, 'comcigan_school', {'code': code,
                'name': os.environ.get('COMCIGAN_SCHOOL_NAME', ''),
                'region': os.environ.get('COMCIGAN_SCHOOL_REGION', '')})


def text(value, label, limit=100, required=True):
    if not isinstance(value, str):
        fail(400, label + ' 입력을 확인하세요.')
    value = value.strip()
    if (required and not value) or len(value) > limit or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value):
        fail(400, f'{label}: {limit}자 이내로 입력하세요.')
    return value


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ':' + digest.hex()


def password_valid(password, stored):
    salt, expected = stored.split(':')
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(actual.hex(), expected)


def setting(db, name, default):
    row = db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
    return json.loads(row['value']) if row else default


def set_setting(db, name, value):
    db.execute('INSERT INTO settings(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
               (name, json.dumps(value, ensure_ascii=False)))


def rate_limit(db, key, maximum, seconds):
    now = time.time()
    db.execute('BEGIN IMMEDIATE')
    db.execute('DELETE FROM limits WHERE started < ?', (now - 3600,))
    row = db.execute('SELECT * FROM limits WHERE key=?', (key,)).fetchone()
    if not row or now - row['started'] >= seconds:
        db.execute('INSERT OR REPLACE INTO limits VALUES (?,?,1)', (key, now))
    elif row['count'] >= maximum:
        db.rollback()
        fail(429, '시도가 너무 많습니다. 잠시 후 다시 시도하세요.')
    else:
        db.execute('UPDATE limits SET count=count+1 WHERE key=?', (key,))
    db.commit()


def allergies(value):
    words = [w.strip() for w in re.split(r'[,，、\n]+', value) if w.strip()]
    return sorted({ALIASES[w] for w in words if w in ALIASES}), [w for w in words if w not in ALIASES]


def neis(service, **parameters):
    key = ''
    if CONFIG_PATH.exists():
        try:
            key = str(json.loads(CONFIG_PATH.read_text(encoding='utf-8')).get('api_key', '')).strip()
        except (OSError, ValueError, AttributeError):
            fail(503, 'config.json의 인증키 설정을 확인하세요.')
    if key == 'YOUR_NEIS_API_KEY':
        key = ''
    parameters.update(Type='json', pIndex=1, pSize=100 if key else 5)
    if key:
        parameters['KEY'] = key
    try:
        with urlopen('https://open.neis.go.kr/hub/' + service + '?' + urlencode(parameters), timeout=2.5) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError):
        fail(502, '나이스 연결에 실패했습니다. 인터넷과 인증키를 확인하세요.')
    if 'RESULT' in result:
        if result['RESULT'].get('CODE') == 'INFO-200':
            return [], not bool(key)
        fail(502, '나이스 조회에 실패했습니다. 인증키와 학교 정보를 확인하세요.')
    return [r for b in result.get(service, []) for r in b.get('row', [])], not bool(key)


def audit(db, actor, action, target=None):
    db.execute('INSERT INTO audit(actor,action,target,created) VALUES (?,?,?,?)', (actor, action, target, time.time()))


def small_int(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        fail(400, label + ' 입력을 확인하세요.')
    return value


def week_start(week=1):
    now = datetime.now(timezone(timedelta(hours=9))).date()
    return now - timedelta(days=now.weekday()) + timedelta(weeks=week - 1)


def selected_class(db, user):
    saved = setting(db, 'class:' + str(user['id']), {})
    if (isinstance(saved, dict) and type(saved.get('grade')) is int
            and 1 <= saved['grade'] <= 3
            and type(saved.get('classroom')) is int
            and 1 <= saved['classroom'] <= 40):
        return saved
    login = user['login']
    if re.fullmatch(r'[1-3][0-9]{4}', login) and 1 <= int(login[1:3]) <= 40:
        return {'grade': int(login[0]), 'classroom': int(login[1:3])}
    return {'grade': 1, 'classroom': 1}


def class_key(selection, start):
    return f"{selection['grade']}-{selection['classroom']}:{start}"


def class_timetable(db, user, week, selection=None, allow_live=False):
    selection = selection or selected_class(db, user)
    start = week_start(week).isoformat()
    entries = setting(db, 'class_timetables', {})
    entry = entries.get(class_key(selection, start))
    if entry:
        metadata = dict(entry['meta'], live=False)
        return selection, start, entry['days'], metadata
    # 학생의 조회는 공유 시간표를 변경하지 않는다. 공개 원본만 읽고 메모리 캐시한다.
    school = setting(db, 'comcigan_school', {})
    if allow_live and school:
        try:
            result = comcigan.normalize(comcigan.get_week(int(school['code']), week),
                                        selection['grade'], selection['classroom'])
            if result['week_start'] != start:
                raise comcigan.ComciganError('컴시간에 공개된 시간표의 주가 선택한 주와 다릅니다.')
            metadata = {key: value for key, value in result.items() if key != 'days'}
            metadata.update(source='컴시간알리미 자동 조회', live=True, school=school,
                            grade=selection['grade'], classroom=selection['classroom'])
            return selection, start, result['days'], metadata
        except (comcigan.ComciganError, ValueError, KeyError) as error:
            return selection, start, {}, {'source': '', 'week_start': start, 'live': False,
                'error': str(error) if isinstance(error, comcigan.ComciganError) else '컴시간 학교 설정을 확인하세요.'}
    legacy = setting(db, 'timetable', {}) if not entries and week == 1 else {}
    return selection, start, legacy, {'source': '기존 공통 시간표' if legacy else '', 'week_start': start, 'live': False}


class BoundedHTTPServer(ThreadingHTTPServer):
    """동시 연결을 제한하고 종료 시 작업 스레드를 정리한다."""
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self.connection_slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SchoolServer'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def reply(self, status, value, mime='application/json; charset=utf-8'):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'")
        if self.new_cookie:
            self.send_header('Set-Cookie', self.new_cookie)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            fail(400, '요청 길이 형식을 확인하세요.')
        if self.headers.get_content_type() != 'application/json':
            fail(400, 'JSON 형식으로 요청하세요.')
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 100_000:
                fail(413, '요청 크기가 너무 큽니다.')
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                fail(400, '입력 형식을 확인하세요.')
            return value
        except (ValueError, UnicodeError):
            fail(400, '입력 형식을 확인하세요.')

    def current_session(self, db):
        raw = ''
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            if 'school_session' in cookies:
                raw = cookies['school_session'].value
        except CookieError:
            pass
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        return db.execute('SELECT * FROM sessions WHERE token=? AND expires>?', (hashed, time.time())).fetchone()

    def new_session(self, db, user_id=None, old=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if old:
            db.execute('DELETE FROM sessions WHERE token=?', (old['token'],))
        db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
        role = db.execute('SELECT role FROM users WHERE id=?', (user_id,)).fetchone() if user_id else None
        lifetime = 1800 if not role or role['role'] == 'admin' else 8 * 3600
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
            (hashlib.sha256(token.encode()).hexdigest(), csrf, user_id, time.time() + lifetime))
        db.commit()
        secure = '; Secure' if PUBLIC_ORIGIN else ''
        self.new_cookie = f'school_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={lifetime}{secure}'
        return csrf

    def user(self, db, login_session, admin=False):
        row = None if not login_session else db.execute('SELECT * FROM users WHERE id=?', (login_session['user_id'],)).fetchone()
        if not row or row['status'] != 'approved':
            fail(401, '로그인하세요. 학생은 관리자 승인 후 로그인할 수 있습니다.')
        if admin and row['role'] != 'admin':
            fail(403, '관리자만 변경할 수 있습니다.')
        return row

    def do_GET(self):
        self.handle_route('GET')

    def do_POST(self):
        self.handle_route('POST')

    def handle_route(self, method):
        self.new_cookie, db = None, None
        try:
            host = self.headers.get('Host', '')
            port = getattr(self.server, 'server_port', DEFAULT_PORT)
            allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            if PUBLIC_ORIGIN:
                allowed_hosts.add(urlsplit(PUBLIC_ORIGIN).netloc)
            if len(self.headers.get_all('Host', [])) != 1 or host not in allowed_hosts:
                fail(403, f'http://127.0.0.1:{port} 으로 접속하세요.')
            if method == 'POST':
                origin = self.headers.get('Origin')
                expected_origin = PUBLIC_ORIGIN if PUBLIC_ORIGIN and host == urlsplit(PUBLIC_ORIGIN).netloc else 'http://' + host
                if (origin and origin != expected_origin) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    fail(403, '다른 사이트에서 보낸 요청입니다.')
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            if method == 'GET' and path == '/healthz':
                with connect() as health_db:
                    health_db.execute('SELECT 1').fetchone()
                return self.reply(200, {'status': 'ok'})
            if path == '/admin/':
                path = '/admin'
            static = {
                '/': ('templates/index.html', 'text/html'),
                '/admin': ('templates/index.html', 'text/html'),
                '/static/script.js': ('static/script.js', 'text/javascript'),
                '/static/style.css': ('static/style.css', 'text/css'),
                '/app.js': ('static/script.js', 'text/javascript'),
                '/style.css': ('static/style.css', 'text/css'),
            }
            if method == 'GET' and path == '/admin':
                db = connect()
                active_session = self.current_session(db)
                active_user = None if not active_session else db.execute(
                    'SELECT role,status FROM users WHERE id=?', (active_session['user_id'],)).fetchone()
                if active_user and active_user['role'] == 'student' and active_user['status'] == 'approved':
                    fail(403, '학생 계정으로 관리자 화면에 접근할 수 없습니다.')
            if method == 'GET' and path in static:
                filename, mime = static[path]
                return self.reply(200, (ROOT / filename).read_bytes(), mime + '; charset=utf-8')
            db = db or connect()
            login_session = self.current_session(db)
            if method == 'GET' and path == '/api/session':
                if not login_session:
                    rate_limit(db, 'session:' + self.client_address[0], 150, 600)
                    csrf = self.new_session(db)
                else:
                    csrf = login_session['csrf']
                user = None if not login_session or login_session['user_id'] is None else db.execute(
                    'SELECT id,login,name,role,status FROM users WHERE id=?', (login_session['user_id'],)).fetchone()
                return self.reply(200, {'csrf': csrf, 'user': dict(user) if user and user['status'] == 'approved' else None,
                    'admin_ready': bool(db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone()), 'version': VERSION})
            body = {}
            if method == 'POST':
                if not login_session or len(self.headers.get_all('X-CSRF-Token', [])) != 1 or not hmac.compare_digest(login_session['csrf'].encode(), self.headers.get('X-CSRF-Token', '').encode()):
                    fail(403, '화면을 새로고침하고 다시 시도하세요.')
                body = self.body()
            if method == 'POST' and path == '/api/register':
                rate_limit(db, 'register:' + self.client_address[0], 10, 3600)
                student_id = text(body.get('student_id'), '학번', 12)
                name = text(body.get('name'), '이름', 30)
                password = body.get('password')
                if not re.fullmatch(r'\d{3,12}', student_id):
                    fail(400, '학번은 숫자 3~12자리로 입력하세요.')
                if not re.fullmatch(r'[가-힣A-Za-z· ]{2,30}', name):
                    fail(400, '이름은 한글 또는 영문 2~30자로 입력하세요.')
                if not isinstance(password, str) or not 4 <= len(password) <= 10:
                    fail(400, '비밀번호는 4~10자로 입력하세요.')
                try:
                    db.execute("INSERT INTO users(login,name,password,role,status,created) VALUES (?,?,?,'student','pending',?)",
                        (student_id, name, password_hash(password), time.time()))
                    db.commit()
                except sqlite3.IntegrityError:
                    fail(409, '이미 신청한 학번입니다. 관리자에게 확인하세요.')
                return self.reply(201, {'message': '가입 신청을 보냈습니다. 관리자가 학번과 이름을 확인하여 승인한 뒤 로그인하세요.'})
            if method == 'POST' and path in {'/api/login/student', '/api/login/admin'}:
                if path == '/api/login/admin' and login_session['user_id']:
                    active_user = db.execute('SELECT role,status FROM users WHERE id=?', (login_session['user_id'],)).fetchone()
                    if active_user and active_user['role'] == 'student' and active_user['status'] == 'approved':
                        fail(403, '학생 계정으로 관리자 로그인할 수 없습니다. 먼저 로그아웃하세요.')
                login = text(body.get('login'), '학번 또는 관리자 ID', 40)
                password = body.get('password')
                if not isinstance(password, str) or not 1 <= len(password) <= 128:
                    fail(400, '비밀번호를 확인하세요.')
                role = path.rsplit('/', 1)[-1]
                rate_limit(db, 'login-ip:' + self.client_address[0], 30, 900)
                rate_limit(db, 'login-account:' + role + ':' + login, 5 if role == 'admin' else 10, 900)
                user = db.execute('SELECT * FROM users WHERE login=? AND role=?', (login, role)).fetchone()
                if not password_valid(password, user['password'] if user else DUMMY_HASH) or not user:
                    fail(401, 'ID 또는 비밀번호를 확인하세요.')
                if user['status'] != 'approved':
                    fail(403, '관리자 승인 대기 중이거나 사용이 제한된 계정입니다. 관리자에게 문의하세요.')
                self.new_session(db, user['id'], login_session)
                return self.reply(200, {'message': '로그인했습니다.'})
            if method == 'POST' and path == '/api/logout':
                self.new_session(db, old=login_session)
                return self.reply(200, {'message': '로그아웃했습니다.'})
            user = self.user(db, login_session, path.startswith('/api/admin/'))
            if method == 'GET' and path == '/api/data':
                known, unknown = allergies(user['allergies'])
                try:
                    week = int(query.get('week', ['1'])[0])
                except ValueError:
                    fail(400, '조회 주를 확인하세요.')
                small_int(week, '조회 주', 2)
                requested_class = None
                if 'grade' in query or 'classroom' in query:
                    try:
                        grade = int(query.get('grade', [''])[0])
                        classroom = int(query.get('classroom', [''])[0])
                    except ValueError:
                        fail(400, '학년과 반을 확인하세요.')
                    requested_class = {'grade': small_int(grade, '학년', 3),
                                       'classroom': small_int(classroom, '반', 40)}
                selection, start, timetable, metadata = class_timetable(db, user, week, requested_class, allow_live=True)
                return self.reply(200, {'school': setting(db, 'school', {}), 'timetable': timetable,
                    'selection': selection, 'week': week, 'week_start': start, 'timetable_meta': metadata,
                    'timetable_available': bool(metadata.get('source')),
                    'timetable_live': bool(metadata.get('live')),
                    'timetable_saved': bool(metadata.get('source')) and not metadata.get('live', False),
                    'allergy_text': user['allergies'], 'unknown_allergies': unknown, 'exams': json.loads(user['exams']), 'comcigan_school': setting(db, 'comcigan_school', {})})
            if method == 'POST' and path == '/api/class':
                selection = {
                    'grade': small_int(body.get('grade'), '학년', 3),
                    'classroom': small_int(body.get('classroom'), '반', 40),
                }
                set_setting(db, 'class:' + str(user['id']), selection)
                db.commit()
                return self.reply(200, {'message': '내가 볼 학년·반을 저장했습니다.'})
            if method == 'GET' and path == '/api/admin/comcigan/schools':
                rate_limit(db, 'comci-search:' + str(user['id']), 30, 600)
                name = text(query.get('name', [''])[0], '학교 이름', 80)
                return self.reply(200, {'schools': comcigan.search_schools(name)})
            if method == 'POST' and path == '/api/admin/comcigan/preview':
                rate_limit(db, 'comci-preview:' + str(user['id']), 20, 600)
                name = text(body.get('name'), '컴시간 학교 이름', 80)
                code = small_int(body.get('code'), '컴시간 학교', 99999999)
                week = small_int(body.get('week'), '조회 주', 2)
                selection = selected_class(db, user)
                result = comcigan.preview(name, code, selection['grade'], selection['classroom'], week)
                if result['week_start'] != week_start(week).isoformat():
                    fail(409, '컴시간에 공개된 시간표의 주가 선택한 주와 다릅니다. 컴시간 원본의 날짜를 확인하세요.')
                # 기존 준비물은 같은 요일·교시에 과목이 같은 경우에만 유지한다.
                entries = setting(db, 'class_timetables', {})
                previous = entries.get(class_key(selection, result['week_start']), {}).get('days', {})
                for day, lessons in result['days'].items():
                    old_lessons = previous.get(day, [])
                    for index, lesson in enumerate(lessons):
                        if index < len(old_lessons) and old_lessons[index]['subject'] == lesson['subject']:
                            lesson['supplies'] = old_lessons[index]['supplies']
                ticket = secrets.token_urlsafe(24)
                set_setting(db, 'comci-preview:' + str(user['id']), {'ticket': ticket,
                    'expires': time.time() + 600, 'result': result})
                db.commit()
                return self.reply(200, {'ticket': ticket, 'preview': result})
            if method == 'POST' and path == '/api/admin/comcigan/import':
                ticket = text(body.get('ticket'), '미리보기', 100)
                db.execute('BEGIN IMMEDIATE')
                saved = setting(db, 'comci-preview:' + str(user['id']), {})
                if not saved or saved['expires'] < time.time() or not hmac.compare_digest(saved['ticket'], ticket):
                    fail(400, '미리보기가 만료되었습니다. 다시 불러온 뒤 저장하세요.')
                result = saved['result']
                selection = selected_class(db, user)
                if selection != {'grade': result['grade'], 'classroom': result['classroom']}:
                    fail(409, '선택한 학급이 바뀌었습니다. 다시 불러오세요.')
                entries = setting(db, 'class_timetables', {})
                key = class_key(selection, result['week_start'])
                metadata = {k: v for k, v in result.items() if k != 'days'}
                metadata['imported_at'] = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec='seconds')
                entries[key] = {'days': result['days'], 'meta': metadata}
                set_setting(db, 'class_timetables', entries)
                set_setting(db, 'comcigan_school', result['school'])
                db.execute('DELETE FROM settings WHERE name=?', ('comci-preview:' + str(user['id']),))
                audit(db, user['id'], 'comcigan:import:' + key)
                db.commit()
                return self.reply(200, {'message': f"{result['grade']}학년 {result['classroom']}반 시간표를 저장하고 학생에게 공개했습니다."})
            if method == 'POST' and path == '/api/allergies':
                value = text(body.get('text'), '알레르기', 500, False)
                if not re.fullmatch(r'[가-힣A-Za-z0-9\s,，、()·.\-]*', value):
                    fail(400, '알레르기는 식품 이름을 쉼표로 구분해 입력하세요.')
                db.execute('UPDATE users SET allergies=? WHERE id=?', (value, user['id']))
                db.commit()
                return self.reply(200, {'message': '내 알레르기를 저장했습니다.'})
            if method == 'GET' and path == '/api/admin/students':
                rows = db.execute("SELECT id,login AS student_id,name,status FROM users WHERE role='student' ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, login").fetchall()
                return self.reply(200, {'students': [dict(row) for row in rows]})
            if method == 'POST' and path == '/api/admin/delete-student':
                target = body.get('id')
                student_id = text(body.get('student_id'), '삭제할 학생 학번', 12)
                if type(target) is not int or target <= 0 or not re.fullmatch(r'\d{3,12}', student_id):
                    fail(400, '삭제할 학생을 확인하세요.')
                db.execute('BEGIN IMMEDIATE')
                target_user = db.execute('SELECT id,login,role FROM users WHERE id=?', (target,)).fetchone()
                if not target_user:
                    fail(404, '이미 삭제되었거나 존재하지 않는 학생입니다.')
                if target_user['role'] != 'student':
                    fail(403, '학생 계정만 삭제할 수 있습니다.')
                if target_user['login'] != student_id:
                    fail(409, '학생 정보가 변경되었습니다. 목록을 새로고침한 뒤 다시 확인하세요.')
                # 외래 키에 연결된 세션부터 지워 기존 로그인도 즉시 해제한다.
                db.execute('DELETE FROM sessions WHERE user_id=?', (target,))
                db.execute('DELETE FROM settings WHERE name IN (?,?)',
                           ('class:' + str(target), 'comci-preview:' + str(target)))
                db.execute("DELETE FROM users WHERE id=? AND login=? AND role='student'", (target, student_id))
                audit(db, user['id'], 'student:delete', target)
                db.commit()
                return self.reply(200, {'message': '학생 계정을 삭제했습니다.'})
            if method == 'POST' and path == '/api/admin/approve':
                target, status = body.get('id'), body.get('status')
                if type(target) is not int or not isinstance(status, str) or status not in {'approved', 'rejected', 'pending'}:
                    fail(400, '승인 요청을 확인하세요.')
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute("UPDATE users SET status=? WHERE id=? AND role='student'", (status, target)).rowcount
                if not changed:
                    fail(404, '학생 계정을 찾을 수 없습니다.')
                db.execute('DELETE FROM sessions WHERE user_id=?', (target,))
                audit(db, user['id'], 'student:' + status, target)
                db.commit()
                return self.reply(200, {'message': '학생의 승인 상태를 변경했습니다.'})
            if method == 'GET' and path == '/api/admin/schools':
                rate_limit(db, 'school-search:' + str(user['id']), 30, 600)
                name = text(query.get('name', [''])[0], '학교 이름', 80)
                if len(name) < 2:
                    fail(400, '학교 이름을 두 글자 이상 입력하세요.')
                rows, sample = neis('schoolInfo', SCHUL_NM=name)
                schools = [{'name': r['SCHUL_NM'], 'address': r.get('ORG_RDNMA', ''),
                    'office': r['ATPT_OFCDC_SC_CODE'], 'code': r['SD_SCHUL_CODE']} for r in rows]
                return self.reply(200, {'schools': schools, 'message': '인증키 미설정: 검색 결과는 최대 5건입니다.' if sample else ''})
            if method == 'POST' and path == '/api/admin/school':
                office, code = text(body.get('office'), '교육청 코드', 8), text(body.get('code'), '학교 코드', 15)
                rows, _ = neis('schoolInfo', ATPT_OFCDC_SC_CODE=office, SD_SCHUL_CODE=code)
                matches = [r for r in rows if r['ATPT_OFCDC_SC_CODE'] == office and r['SD_SCHUL_CODE'] == code]
                if len(matches) != 1:
                    fail(400, '학교 검색 결과에서 선택하세요.')
                row = matches[0]
                set_setting(db, 'school', {'name': row['SCHUL_NM'], 'address': row.get('ORG_RDNMA', ''), 'office': office, 'code': code})
                audit(db, user['id'], 'school:save')
                db.commit()
                return self.reply(200, {'message': '학교 정보를 저장했습니다.'})
            if method == 'POST' and path == '/api/admin/timetable':
                day, lessons = str(body.get('day', '')), body.get('lessons')
                if day not in map(str, range(7)) or not isinstance(lessons, list) or len(lessons) > 12:
                    fail(400, '요일과 수업 수를 확인하세요.')
                cleaned = []
                for lesson in lessons:
                    if not isinstance(lesson, dict):
                        fail(400, '수업 입력을 확인하세요.')
                    cleaned.append({'subject': text(lesson.get('subject'), '과목', 80),
                        'supplies': text(lesson.get('supplies'), '준비물', 200, False)})
                week = small_int(body.get('week', 1), '조회 주', 2)
                selection, start, timetable, metadata = class_timetable(db, user, week, allow_live=True)
                db.execute('BEGIN IMMEDIATE')
                entries = setting(db, 'class_timetables', {})
                key = class_key(selection, start)
                if key in entries:
                    timetable, metadata = entries[key]['days'], entries[key]['meta']
                timetable[day] = cleaned
                metadata.pop('live', None)
                metadata.pop('error', None)
                metadata['source'] = metadata.get('source') or '관리자 직접 입력'
                metadata['last_edited'] = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec='seconds')
                entries[key] = {'days': timetable, 'meta': metadata}
                set_setting(db, 'class_timetables', entries)
                audit(db, user['id'], 'timetable:save:' + key + ':' + day)
                db.commit()
                return self.reply(200, {'message': '시간표를 저장했습니다.'})
            if method == 'POST' and path == '/api/exams':
                exams = body.get('exams')
                if not isinstance(exams, list) or len(exams) > 100:
                    fail(400, '시험 목록을 확인하세요.')
                cleaned = []
                for exam in exams:
                    if not isinstance(exam, dict):
                        fail(400, '시험 입력을 확인하세요.')
                    name = text(exam.get('name'), '시험 이름', 100)
                    try:
                        target = date.fromisoformat(exam['date']).isoformat()
                    except (ValueError, TypeError, KeyError):
                        fail(400, '시험 날짜를 확인하세요.')
                    ranges = exam.get('ranges')
                    if not isinstance(ranges, list) or len(ranges) > 100:
                        fail(400, '공부 범위를 확인하세요.')
                    items = []
                    for item in ranges:
                        if not isinstance(item, dict) or type(item.get('done')) is not bool:
                            fail(400, '공부 완료 상태를 확인하세요.')
                        items.append({'subject': text(item.get('subject'), '과목', 80),
                            'scope': text(item.get('scope'), '공부 범위', 300), 'done': item['done']})
                    cleaned.append({'name': name, 'date': target, 'ranges': items})
                db.execute('UPDATE users SET exams=? WHERE id=?', (json.dumps(cleaned, ensure_ascii=False), user['id']))
                db.commit()
                return self.reply(200, {'message': '내 시험 계획을 저장했습니다.'})
            if method == 'GET' and path == '/api/meals':
                rate_limit(db, 'meals:' + str(user['id']), 90, 600)
                school = setting(db, 'school', {})
                if not school:
                    return self.reply(200, {'meals': [], 'message': '관리자가 학교를 등록하면 급식을 확인할 수 있습니다.'})
                try:
                    target = date.fromisoformat(query.get('date', [''])[0])
                except ValueError:
                    fail(400, '날짜를 확인하세요.')
                rows, _ = neis('mealServiceDietInfo', ATPT_OFCDC_SC_CODE=school['office'], SD_SCHUL_CODE=school['code'], MLSV_YMD=target.strftime('%Y%m%d'))
                known, unknown = allergies(user['allergies'])
                meals = []
                for row in rows:
                    items = []
                    for raw in re.split(r'<br\s*/?>', row.get('DDISH_NM', ''), flags=re.I):
                        name = html.unescape(re.sub(r'<[^>]+>', '', raw)).strip()
                        if name:
                            matches = sorted(set(map(int, re.findall(r'\d+', name))).intersection(known))
                            items.append({'name': name, 'warnings': [ALLERGENS[n] for n in matches]})
                    meals.append({'type': row.get('MMEAL_SC_NM', '급식'), 'items': items})
                return self.reply(200, {'meals': meals, 'message': '' if meals else '해당 날짜에 등록된 급식이 없습니다.'})
            fail(404, '요청한 기능을 찾을 수 없습니다.')
        except comcigan.ComciganError as error:
            self.reply(502, {'error': str(error)})
        except ApiError as error:
            self.reply(error.status, {'error': error.message})
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError):
            self.reply(500, {'error': '처리하지 못했습니다. 입력을 확인하거나 관리자에게 문의하세요.'})
        finally:
            if db:
                db.close()


DUMMY_HASH = password_hash('dummy-password-for-timing')


def main():
    parser = argparse.ArgumentParser(description='오늘의 학교 v2')
    parser.add_argument('--create-admin', action='store_true', help='최초 관리자 계정 생성')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', DEFAULT_PORT)),
                        help=f'로컬 서버 포트 (기본 {DEFAULT_PORT})')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('포트는 1~65535 사이의 숫자여야 합니다.')
    required_files = ('templates/index.html', 'static/script.js', 'static/style.css')
    missing = [name for name in required_files if not (ROOT / name).is_file()]
    if missing and not args.create_admin:
        print('서버 시작 실패: 필요한 화면 파일이 없습니다.')
        for name in missing:
            print(f'  누락: {ROOT / name}')
        return 1
    if PUBLIC_ORIGIN and not os.environ.get('DATABASE_URL', '').strip():
        parser.error('DATABASE_URL is required for hosted deployments')
    initialize()
    if not args.create_admin:
        initialize_hosted_settings()
    if args.create_admin:
        with connect() as db:
            if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
                print('관리자 계정이 이미 있습니다. 추가 생성하지 않았습니다.')
                return
            login = input('관리자 ID (영문/숫자 3~30자): ').strip()
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{2,29}', login):
                print('영문자로 시작하는 영문/숫자/_ 3~30자 ID가 필요합니다.')
                return
            password = getpass.getpass('관리자 비밀번호 (4~10자): ')
            confirmation = getpass.getpass('비밀번호 다시 입력: ')
            if not 4 <= len(password) <= 10 or password != confirmation:
                print('비밀번호 길이 또는 일치 여부를 확인하세요.')
                return
            db.execute("INSERT INTO users(login,name,password,role,status,created) VALUES (?,'관리자',?,'admin','approved',?)",
                (login, password_hash(password), time.time()))
            db.commit()
            print('관리자를 생성했습니다. python app.py 실행 후 /admin에서 로그인하세요.')
        return
    try:
        server = BoundedHTTPServer(('0.0.0.0' if PUBLIC_ORIGIN else '127.0.0.1', args.port), Handler)
    except OSError as error:
        if error.errno in {48, 98, 10048} or getattr(error, 'winerror', None) == 10048:
            print(f'서버 시작 실패: {args.port}번 포트를 다른 프로그램이 사용 중입니다.')
            print('기존 서버를 실행한 터미널에서 Ctrl+C로 종료한 뒤 다시 실행하세요.')
            print(f'또는 다른 포트로 실행하세요: python app.py --port {5002 if args.port != 5002 else 5003}')
        else:
            print(f'서버 시작 실패: {args.port}번 포트를 열 수 없습니다. 오류: {error}')
        return 1
    print('오늘의 학교 ' + VERSION)
    print(f'실행 프로젝트: {ROOT}')
    print(f'학생: http://127.0.0.1:{args.port}/')
    print(f'관리자: http://127.0.0.1:{args.port}/admin')
    print('최초 관리자 생성: python app.py --create-admin / 종료: Ctrl+C')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    raise SystemExit(main())
