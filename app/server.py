"""Read-only LLM Dashboard and authenticated configuration API."""
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import ssl
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse
from checks import CANDY_PROMPT, inspect_svg
from database import Database
from metrics import Metrics
from repositories import Repository
from workers import Scheduler

ROOT = Path(__file__).resolve().parent
SESSION_COOKIE = 'llm_dashboard_admin_session'
SESSION_TTL = 7200


class Auth:
    def __init__(self, database):
        self.database = database
        self.lock = threading.RLock()
        self.sessions, self.failures = {}, []

    def configured(self):
        with self.database.connect() as db:
            return bool(db.execute('SELECT 1 FROM admin_auth WHERE id=1').fetchone())

    def setup(self, password):
        if not isinstance(password,str) or not 12<=len(password)<=256:
            raise ValueError('管理密码需要 12–256 个字符')
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
        with self.lock,self.database.connect() as db:
            if db.execute('SELECT 1 FROM admin_auth WHERE id=1').fetchone():
                raise ValueError('管理密码已设置')
            db.execute('INSERT INTO admin_auth VALUES(1,?,?)',(salt,digest))

    def login(self, password):
        with self.lock:
            now = time.time()
            self.failures = [v for v in self.failures if v>now-60]
            if len(self.failures)>=8:
                raise ValueError('登录失败次数过多，请一分钟后重试')
            with self.database.connect() as db:
                row = db.execute('SELECT * FROM admin_auth WHERE id=1').fetchone()
            if row and isinstance(password,str) and len(password)<=256:
                digest = hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(row['salt']),600000).hex()
                valid = hmac.compare_digest(digest,row['password_hash'])
            else:
                valid = False
            if not valid:
                self.failures.append(now)
                raise ValueError('管理密码不正确')
            self.failures.clear()
            self.sessions = {k:v for k,v in self.sessions.items() if v>now}
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now+SESSION_TTL
            return token

    def expires_at(self, token):
        with self.lock:
            expires = self.sessions.get(token)
            if expires is not None and expires>time.time():
                return expires
            self.sessions.pop(token,None)
            return None

    def valid(self, token):
        return self.expires_at(token) is not None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token,None)


class Application:
    def __init__(self, directory):
        self.database = Database(directory)
        self.auth = Auth(self.database)
        self.repository = Repository(self.database)
        self.metrics = Metrics(self.database)
        self.scheduler = Scheduler(self.database,self.metrics)
        self.repository.changed = self.scheduler.changed


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass

    @property
    def app(self):
        return self.server.app

    @property
    def cookie_token(self):
        raw = '; '.join(self.headers.get_all('Cookie',[]))
        if not raw or len(raw)>8192 or any(ord(char)<32 or ord(char)==127 for char in raw):
            return ''
        # Reject ambiguous duplicate session cookies rather than choosing one.
        if len(re.findall(r'(?:^|;)\s*'+SESSION_COOKIE+r'\s*=',raw))!=1:
            return ''
        cookies = SimpleCookie()
        try:
            cookies.load(raw)
        except CookieError:
            return ''
        item = cookies.get(SESSION_COOKIE)
        value = item.value if item else ''
        return value if re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value) else ''

    @property
    def token(self):
        authorization = self.headers.get_all('Authorization',[])
        if authorization:
            # An explicit invalid Bearer must never inherit browser Cookie rights.
            if len(authorization)!=1:
                return ''
            match = re.fullmatch(r'Bearer[ \t]+([A-Za-z0-9_-]{1,128})',authorization[0],re.I)
            return match[1] if match else ''
        return self.cookie_token

    def session_cookie(self, token=None):
        cookies = SimpleCookie()
        cookies[SESSION_COOKIE] = token or ''
        cookie = cookies[SESSION_COOKIE]
        cookie['path'] = '/'
        cookie['httponly'] = True
        cookie['samesite'] = 'Lax'
        cookie['max-age'] = SESSION_TTL if token else 0
        if not token:
            cookie['expires'] = 'Thu, 01 Jan 1970 00:00:00 GMT'
        # Do not treat client-supplied forwarding headers as proof of TLS.
        if isinstance(self.connection,ssl.SSLSocket):
            cookie['secure'] = True
        return cookie.OutputString()

    def cross_site_write(self):
        if self.headers.get('Sec-Fetch-Site','').lower()=='cross-site':
            return True
        origins = self.headers.get_all('Origin',[])
        if not origins:
            return False
        if len(origins)!=1:
            return True
        try:
            origin = parse.urlsplit(origins[0])
            return (origin.scheme not in ('http','https') or origin.netloc!=self.headers.get('Host')
                    or bool(origin.path or origin.query or origin.fragment or origin.username or origin.password))
        except ValueError:
            return True

    def send(self, data, content_type='application/json; charset=utf-8', status=200, svg=False, cache=False, headers=None):
        if not isinstance(data,bytes):
            data = json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        etag = '"'+hashlib.sha256(data).hexdigest()[:24]+'"' if cache else None
        if etag and self.headers.get('If-None-Match')==etag:
            self.send_response(304)
            self.send_header('ETag',etag)
            self.send_header('Cache-Control','private, max-age=60')
            self.send_header('Content-Length','0')
            self.end_headers()
            return
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','private, max-age=60' if cache else 'no-store')
        if cache:
            self.send_header('ETag',etag)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        policy = "sandbox; default-src 'none'; style-src 'unsafe-inline'" if svg else "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        self.send_header('Content-Security-Policy',policy)
        for name,value in (headers or {}).items():
            self.send_header(name,value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):
            pass

    def local_setup(self):
        host = parse.urlsplit('http://'+self.headers.get('Host','')).hostname
        return self.server.server_address[0] in ('127.0.0.1','::1') and self.client_address[0] in ('127.0.0.1','::1') and host in ('127.0.0.1','localhost','::1') and not self.headers.get('Forwarded') and not self.headers.get('X-Forwarded-For')

    def do_GET(self):
        try:
            self.get()
        except (ValueError,TypeError) as exc:
            self.send({'error':str(exc)},status=400)
        except Exception:
            logging.exception('Read request failed')
            self.send({'error':'服务处理异常'},status=500)

    def get(self):
        url = parse.urlsplit(self.path)
        path,params = url.path,parse.parse_qs(url.query)
        if path.startswith('/api/guest/'):
            return self.send({'error':'访客检测已关闭；本站为只读监控大盘'},status=410)
        if path=='/api/auth/status':
            expires = self.app.auth.expires_at(self.token)
            return self.send({'configured':self.app.auth.configured(),'setup_allowed':self.local_setup(),
                              'authenticated':expires is not None,'expires_at':expires})
        if path.startswith('/api/admin/'):
            if not self.app.auth.valid(self.token):
                return self.send({'error':'请先登录管理员后台'},status=401)
            if path=='/api/admin/models':
                return self.send({'models':self.app.repository.models(),'categories':self.app.repository.categories(),'templates':self.app.repository.templates()})
            if path=='/api/admin/cards':
                return self.send({'cards':self.app.repository.channels(),'categories':self.app.repository.categories(),'templates':self.app.repository.templates()})
            if path=='/api/admin/templates':
                return self.send(self.app.repository.templates())
            if path=='/api/admin/categories':
                return self.send(self.app.repository.categories())
            if path=='/api/admin/channels':
                return self.send(self.app.repository.channels())
            if path=='/api/admin/tasks':
                return self.send(self.app.repository.tasks())
            match = re.fullmatch(r'/api/admin/runs/([1-9][0-9]{0,17})',path)
            if match:
                result = self.app.repository.run(int(match[1]),admin=True)
                return self.send(result or {'error':'记录不存在'},status=200 if result else 404)
        if path=='/api/dashboard':
            hours = int(params.get('hours',['24'])[0])
            if hours not in (1,6,24):
                raise ValueError('统计窗口支持 1、6、24 小时')
            return self.send(self.app.metrics.dashboard(hours))
        match = re.fullmatch(r'/api/(cards|channels|models)/([1-9][0-9]{0,17})/(history|runs)',path)
        if match:
            limit = int(params.get('limit',['50'])[0])
            if not 1<=limit<=100:
                raise ValueError('分页数量支持 1–100')
            kind = params.get('kind',[None])[0]
            if kind not in (None,'candy','pelican'):
                raise ValueError('检测项目不正确')
            before = int(params['before'][0]) if 'before' in params else None
            if before is not None and not 1 <= before < 2**63:
                raise ValueError('分页游标无效')
            return self.send(self.app.repository.history(channel_id=int(match[2]) if match[1] in ('channels','cards') else None,
                            model_id=int(match[2]) if match[1]=='models' else None,kind=kind,before=before,limit=limit))
        match = re.fullmatch(r'/api/runs/([1-9][0-9]{0,17})(/svg)?',path)
        if match:
            rid = int(match[1])
            if match[2]:
                with self.app.database.connect() as db:
                    row = self.app.repository.visible(db,rid)
                    artifact = db.execute('SELECT * FROM artifacts WHERE run_id=?',(rid,)).fetchone() if row else None
                if artifact:
                    file = self.app.scheduler.storage.path(artifact['svg_path'])
                    if file:
                        return self.send(file.read_bytes(),'image/svg+xml; charset=utf-8',svg=True,cache=True)
                if row and row['legacy_svg']:
                    svg,checks,_ = inspect_svg(row['legacy_svg'],'')
                    if checks['svg']:
                        return self.send(svg.encode(),'image/svg+xml; charset=utf-8',svg=True,cache=True)
            else:
                result = self.app.repository.run(rid)
                if result:
                    if result['kind']=='candy' and not result['prompt']:
                        result['prompt'] = CANDY_PROMPT
                    return self.send(result)
            return self.send({'error':'结果不存在或未公开'},status=404)
        match = re.fullmatch(r'/api/artifacts/([1-9][0-9]{0,17})/(thumbnail|svg)',path)
        if match:
            with self.app.database.connect() as db:
                artifact = db.execute('SELECT * FROM artifacts WHERE id=?',(int(match[1]),)).fetchone()
                visible = self.app.repository.visible(db,artifact['run_id']) if artifact else None
            if artifact and visible:
                file = self.app.scheduler.storage.path(artifact['thumbnail_path' if match[2]=='thumbnail' else 'svg_path'])
                if file:
                    is_svg = match[2]=='svg'
                    return self.send(file.read_bytes(),'image/svg+xml; charset=utf-8' if is_svg else 'image/png',svg=is_svg,cache=True)
            return self.send({'error':'图片不存在或未公开'},status=404)
        files = {'/':'index.html','/admin':'admin.html','/admin/':'admin.html',
                 '/app.js':'app.js','/admin.js':'admin.js','/ui.js':'ui.js','/style.css':'style.css','/favicon.svg':'favicon.svg'}
        name = files.get(path)
        if name:
            mime = {'html':'text/html','js':'text/javascript','css':'text/css','svg':'image/svg+xml'}[name.split('.')[-1]]
            return self.send((ROOT/'web'/name).read_bytes(),mime+'; charset=utf-8')
        self.send({'error':'页面或接口不存在'},status=404)

    def do_POST(self):
        self.write_request()

    def do_PATCH(self):
        self.write_request()

    def do_DELETE(self):
        self.write_request()

    def write_request(self):
        path = parse.urlsplit(self.path).path
        if path.startswith('/api/guest/'):
            return self.send({'error':'访客检测已关闭'},status=410)
        if path not in ('/api/auth/login','/api/auth/setup','/api/auth/logout') and not self.app.auth.valid(self.token):
            return self.send({'error':'请先登录管理员后台'},status=401)
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':
            return self.send({'error':'仅接受 JSON 请求'},status=415)
        if self.cross_site_write():
            return self.send({'error':'不接受跨站请求'},status=403)
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0<size<=(65536 if path.startswith('/api/admin/templates') else 16384):
                raise ValueError('请求大小无效')
            values = json.loads(self.rfile.read(size))
            if not isinstance(values,dict):
                raise ValueError('请求必须为 JSON 对象')
            if self.command=='DELETE' and not re.fullmatch(r'/api/admin/(?:categories|templates)/([1-9][0-9]{0,17})',path):
                return self.send({'error':'不支持的操作'},status=405)
            if path=='/api/auth/setup':
                if not self.local_setup():
                    return self.send({'error':'首次密码需在本机设置，或使用 ADMIN_TOKEN 初始化'},status=403)
                self.app.auth.setup(values.get('password'))
                return self.send({'configured':True})
            if path=='/api/auth/login':
                try:
                    token = self.app.auth.login(values.get('password'))
                    return self.send({'token':token,'expires_at':self.app.auth.expires_at(token),'expires_in':SESSION_TTL},
                                     headers={'Set-Cookie':self.session_cookie(token)})
                except ValueError as exc:
                    return self.send({'error':str(exc)},status=401)
            if path=='/api/auth/logout':
                self.app.auth.logout(self.token)
                # Clearing the browser cookie also revokes that session when an
                # API client supplied a different Authorization credential.
                self.app.auth.logout(self.cookie_token)
                return self.send({'ok':True},headers={'Set-Cookie':self.session_cookie()})
            if path=='/api/admin/templates' and self.command=='POST':
                return self.send(self.app.repository.save_template(values),status=201)
            match = re.fullmatch(r'/api/admin/templates/([1-9][0-9]{0,17})',path)
            if match:
                if self.command=='DELETE':
                    return self.send(self.app.repository.archive_template(int(match[1])))
                if self.command=='PATCH':
                    return self.send(self.app.repository.save_template(values,int(match[1])))
                return self.send({'error':'不支持的操作'},status=405)
            if path=='/api/admin/models' and self.command=='POST':
                return self.send(self.app.repository.save_config(values),status=201)
            if path=='/api/admin/cards' and self.command=='POST':
                return self.send(self.app.repository.save_channel(values),status=201)
            match = re.fullmatch(r'/api/admin/cards/([1-9][0-9]{0,17})(/models)?',path)
            if match:
                if match[2] and self.command=='POST':
                    return self.send(self.app.repository.save_card_model(values,channel_id=int(match[1])),status=201)
                if not match[2] and self.command=='PATCH':
                    return self.send(self.app.repository.save_channel(values,int(match[1])))
                return self.send({'error':'不支持的操作'},status=405)
            if path=='/api/admin/categories' and self.command=='POST':
                return self.send(self.app.repository.save_category(values),status=201)
            match = re.fullmatch(r'/api/admin/categories/([1-9][0-9]{0,17})',path)
            if match:
                if self.command=='DELETE':
                    return self.send(self.app.repository.delete_category(int(match[1])))
                if self.command=='PATCH':
                    return self.send(self.app.repository.save_category(values,int(match[1])))
                return self.send({'error':'不支持的操作'},status=405)
            if path=='/api/admin/channels' and self.command=='POST':
                return self.send(self.app.repository.save_channel(values),status=201)
            match = re.fullmatch(r'/api/admin/channels/([1-9][0-9]{0,17})',path)
            if match:
                return self.send(self.app.repository.save_channel(values,int(match[1])))
            match = re.fullmatch(r'/api/admin/channels/([1-9][0-9]{0,17})/models',path)
            if match:
                return self.send(self.app.repository.save_model(values,channel_id=int(match[1])),status=201)
            match = re.fullmatch(r'/api/admin/models/([1-9][0-9]{0,17})(/run)?',path)
            if match:
                if match[2]:
                    kinds = values.get('kinds')
                    if kinds is not None and (not isinstance(kinds,list) or not all(isinstance(k,str) for k in kinds)):
                        raise ValueError('检测项目格式不正确')
                    return self.send({'ids':self.app.scheduler.enqueue(int(match[1]),kinds=kinds)},status=202)
                return self.send(self.app.repository.save_card_model(values,model_id=int(match[1])))
            self.send({'error':'接口不存在'},status=404)
        except (ValueError,TypeError) as exc:
            self.send({'error':str(exc)},status=400)
        except Exception:
            logging.exception('Write request failed')
            self.send({'error':'服务处理异常'},status=500)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    directory = Path(os.environ.get('DATA_DIR',str(ROOT/'data')))
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    instance_lock = (directory/'server.lock').open('a')
    try:
        fcntl.flock(instance_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('同一数据目录已有服务运行') from None
    app = Application(directory)
    token = os.environ.get('ADMIN_TOKEN','')
    if token and not app.auth.configured():
        app.auth.setup(token)
    host,port = os.environ.get('HOST','127.0.0.1'),int(os.environ.get('PORT','8765'))
    if host not in ('127.0.0.1','localhost') and not app.auth.configured():
        raise SystemExit('请使用 ADMIN_TOKEN 初始化管理密码')
    server = Server((host,port),Handler)
    server.app = app
    app.scheduler.start()
    def shutdown(*_):
        app.scheduler.stop()
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown)
    signal.signal(signal.SIGINT,shutdown)
    logging.info('LLM Dashboard listening on %s:%s',host,port)
    try:
        server.serve_forever()
    finally:
        app.scheduler.stop()
        server.server_close()


if __name__=='__main__':
    main()
