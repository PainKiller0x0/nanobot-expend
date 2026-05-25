use std::collections::HashMap;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Clone)]
struct Config {
    bind: String,
    username: String,
    password: String,
    session_file: PathBuf,
    cookie_name: String,
    cookie_domain: String,
    session_ttl: u64,
}

struct Request {
    method: String,
    target: String,
    headers: HashMap<String, String>,
    body: String,
}

fn main() {
    let config = Config::load();
    if let Some(parent) = config.session_file.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let listener = TcpListener::bind(&config.bind)
        .unwrap_or_else(|e| panic!("failed to bind {}: {}", config.bind, e));
    eprintln!("nanobot-auth-gate listening on {}", config.bind);

    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let config = config.clone();
                thread::spawn(move || {
                    let _ = handle_connection(stream, &config);
                });
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
}

impl Config {
    fn load() -> Self {
        let password_file = env::var("AUTH_GATE_PASSWORD_FILE")
            .unwrap_or_else(|_| "/root/.nanobot/caddy_basic_auth.txt".to_string());
        Self {
            bind: env::var("AUTH_GATE_BIND").unwrap_or_else(|_| "127.0.0.1:8787".to_string()),
            username: env::var("AUTH_GATE_USERNAME").unwrap_or_else(|_| "nanobot".to_string()),
            password: env::var("AUTH_GATE_PASSWORD")
                .ok()
                .or_else(|| read_password_file(&password_file))
                .unwrap_or_else(|| panic!("missing password in AUTH_GATE_PASSWORD or {password_file}")),
            session_file: PathBuf::from(
                env::var("AUTH_GATE_SESSION_FILE")
                    .unwrap_or_else(|_| "/root/.nanobot/auth-gate/sessions.tsv".to_string()),
            ),
            cookie_name: env::var("AUTH_GATE_COOKIE_NAME").unwrap_or_else(|_| "nb_gate".to_string()),
            cookie_domain: env::var("AUTH_GATE_COOKIE_DOMAIN")
                .unwrap_or_else(|_| ".painkiller.top".to_string()),
            session_ttl: env::var("AUTH_GATE_SESSION_TTL")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(7 * 24 * 3600),
        }
    }
}

fn handle_connection(mut stream: TcpStream, config: &Config) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    let Some(req) = read_request(&mut stream)? else {
        return Ok(());
    };
    let response = route(req, config);
    stream.write_all(response.as_bytes())?;
    stream.flush()?;
    Ok(())
}

fn read_request(stream: &mut TcpStream) -> std::io::Result<Option<Request>> {
    let mut buf = Vec::new();
    let mut tmp = [0u8; 4096];
    let mut header_end = None;
    while buf.len() < 128 * 1024 {
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(pos) = find_bytes(&buf, b"\r\n\r\n") {
            header_end = Some(pos + 4);
            break;
        }
    }
    let Some(header_end) = header_end else {
        return Ok(None);
    };

    let head = String::from_utf8_lossy(&buf[..header_end]).to_string();
    let mut lines = head.split("\r\n");
    let Some(first) = lines.next() else {
        return Ok(None);
    };
    let mut first_parts = first.split_whitespace();
    let method = first_parts.next().unwrap_or("").to_string();
    let target = first_parts.next().unwrap_or("/").to_string();
    let mut headers = HashMap::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }
    let content_len = headers
        .get("content-length")
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(0);
    while buf.len() < header_end + content_len && buf.len() < 256 * 1024 {
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&tmp[..n]);
    }
    let body = String::from_utf8_lossy(
        &buf[header_end..std::cmp::min(buf.len(), header_end + content_len)],
    )
    .to_string();
    Ok(Some(Request {
        method,
        target,
        headers,
        body,
    }))
}

fn route(req: Request, config: &Config) -> String {
    let path = req.target.split('?').next().unwrap_or("/");
    match (req.method.as_str(), path) {
        ("GET", "/auth/check") => handle_check(&req, config),
        ("GET", "/auth/login") => {
            let next = query_param(&req.target, "next").unwrap_or_else(|| "/".to_string());
            html_response(200, &login_page(config, &safe_next(&next), ""))
        }
        ("POST", "/auth/login") => handle_login(&req, config),
        ("GET", "/auth/logout") | ("POST", "/auth/logout") => handle_logout(&req, config),
        _ if path.starts_with("/auth/") => redirect("/auth/login"),
        _ => not_found(),
    }
}

fn handle_check(req: &Request, config: &Config) -> String {
    let cookie = req.headers.get("cookie").and_then(|raw| cookie_value(raw, &config.cookie_name));
    if let Some(token) = cookie {
        if token_is_valid(config, &token) {
            return empty_response(204);
        }
    }
    let next = req
        .headers
        .get("x-forwarded-uri")
        .map(String::as_str)
        .unwrap_or("/");
    redirect(&format!("/auth/login?next={}", url_encode(&safe_next(next))))
}

fn handle_login(req: &Request, config: &Config) -> String {
    let form = parse_form(&req.body);
    let user = form.get("username").map(String::as_str).unwrap_or("");
    let pass = form.get("password").map(String::as_str).unwrap_or("");
    let next = safe_next(form.get("next").map(String::as_str).unwrap_or("/"));
    if user == config.username && pass == config.password {
        let token = new_token();
        let expiry = now_epoch() + config.session_ttl;
        save_session(config, &token, expiry, user);
        let cookie = format!(
            "{}={}; Domain={}; Path=/; Max-Age={}; HttpOnly; Secure; SameSite=Lax",
            config.cookie_name, token, config.cookie_domain, config.session_ttl
        );
        return response_with_headers(
            303,
            "See Other",
            &[("Location", next.as_str()), ("Set-Cookie", cookie.as_str())],
            "",
            "text/plain; charset=utf-8",
        );
    }
    html_response(
        401,
        &login_page(config, &next, "账号或密码不对。再试一次，别慌。"),
    )
}

fn handle_logout(req: &Request, config: &Config) -> String {
    if let Some(token) = req
        .headers
        .get("cookie")
        .and_then(|raw| cookie_value(raw, &config.cookie_name))
    {
        remove_session(config, &token);
    }
    let expired = format!(
        "{}=; Domain={}; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
        config.cookie_name, config.cookie_domain
    );
    response_with_headers(
        303,
        "See Other",
        &[("Location", "/auth/login"), ("Set-Cookie", expired.as_str())],
        "",
        "text/plain; charset=utf-8",
    )
}

fn login_page(config: &Config, next: &str, error: &str) -> String {
    let error_html = if error.is_empty() {
        String::new()
    } else {
        format!("<div class=\"error\">{}</div>", escape_html(error))
    };
    format!(
        r#"<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex,nofollow,noarchive" />
  <title>Nanobot 登录</title>
  <style>
    :root {{ color-scheme: dark; --bg:#08130f; --panel:#13231b; --line:#2d4b3b; --text:#ecfff4; --muted:#a9c8b7; --accent:#83f0aa; --bad:#ff8d8d; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--text); background:
      radial-gradient(circle at 20% 10%, rgba(131,240,170,.22), transparent 32rem),
      radial-gradient(circle at 80% 80%, rgba(255,199,102,.14), transparent 28rem),
      linear-gradient(135deg, #07110d, #0b1712 55%, #10130e); }}
    .card {{ width:min(92vw, 440px); padding:34px; border:1px solid var(--line); border-radius:30px; background:rgba(19,35,27,.86); box-shadow:0 30px 80px rgba(0,0,0,.38); backdrop-filter: blur(12px); }}
    .eyebrow {{ letter-spacing:.28em; text-transform:uppercase; color:var(--accent); font-weight:800; font-size:12px; }}
    h1 {{ margin:12px 0 8px; font-size:36px; line-height:1.05; }}
    p {{ margin:0 0 24px; color:var(--muted); line-height:1.7; }}
    label {{ display:block; margin:16px 0 8px; color:var(--muted); font-weight:700; }}
    input {{ width:100%; padding:14px 16px; border-radius:16px; border:1px solid var(--line); background:#091510; color:var(--text); outline:none; font-size:16px; }}
    input:focus {{ border-color:var(--accent); box-shadow:0 0 0 4px rgba(131,240,170,.12); }}
    button {{ width:100%; margin-top:22px; padding:14px 16px; border:0; border-radius:16px; background:var(--accent); color:#07110d; font-weight:900; font-size:16px; cursor:pointer; }}
    .error {{ margin:18px 0 0; padding:12px 14px; border:1px solid rgba(255,141,141,.4); border-radius:16px; color:#ffd0d0; background:rgba(255,141,141,.09); }}
    .foot {{ margin-top:18px; font-size:12px; color:#7fa08d; }}
  </style>
</head>
<body>
  <main class="card">
    <div class="eyebrow">Nanobot Gateway</div>
    <h1>欢迎回来</h1>
    <p>登录后进入你的 nanobot 控制台。公网入口已经收口，旧端口可以慢慢撤掉。</p>
    <form method="post" action="/auth/login">
      <input type="hidden" name="next" value="{next}" />
      <label>账号</label>
      <input name="username" autocomplete="username" value="{user}" autofocus />
      <label>密码</label>
      <input name="password" type="password" autocomplete="current-password" />
      <button type="submit">进入 Nanobot</button>
    </form>
    {error}
    <div class="foot">会话保存在浏览器 Cookie 中，可在 /auth/logout 退出。</div>
  </main>
</body>
</html>"#,
        next = escape_html(next),
        user = escape_html(&config.username),
        error = error_html
    )
}

fn token_is_valid(config: &Config, token: &str) -> bool {
    let now = now_epoch();
    let sessions = read_sessions(config);
    let mut keep = Vec::new();
    let mut ok = false;
    for (stored, expiry, user) in sessions {
        if expiry > now {
            if stored == token {
                ok = true;
            }
            keep.push((stored, expiry, user));
        }
    }
    write_sessions(config, &keep);
    ok
}

fn save_session(config: &Config, token: &str, expiry: u64, user: &str) {
    let mut sessions: Vec<_> = read_sessions(config)
        .into_iter()
        .filter(|(_, exp, _)| *exp > now_epoch())
        .collect();
    sessions.push((token.to_string(), expiry, user.to_string()));
    write_sessions(config, &sessions);
}

fn remove_session(config: &Config, token: &str) {
    let sessions: Vec<_> = read_sessions(config)
        .into_iter()
        .filter(|(stored, _, _)| stored != token)
        .collect();
    write_sessions(config, &sessions);
}

fn read_sessions(config: &Config) -> Vec<(String, u64, String)> {
    let raw = fs::read_to_string(&config.session_file).unwrap_or_default();
    raw.lines()
        .filter_map(|line| {
            let mut parts = line.split('\t');
            let token = parts.next()?.to_string();
            let expiry = parts.next()?.parse().ok()?;
            let user = parts.next().unwrap_or("").to_string();
            Some((token, expiry, user))
        })
        .collect()
}

fn write_sessions(config: &Config, sessions: &[(String, u64, String)]) {
    if let Some(parent) = config.session_file.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let tmp = config.session_file.with_extension("tmp");
    let mut file = File::create(&tmp).expect("create session tmp");
    for (token, expiry, user) in sessions {
        let _ = writeln!(file, "{}\t{}\t{}", token, expiry, user);
    }
    let _ = fs::rename(tmp, &config.session_file);
}

fn read_password_file(path: &str) -> Option<String> {
    let raw = fs::read_to_string(path).ok()?;
    for line in raw.lines() {
        if let Some(value) = line.strip_prefix("password=") {
            return Some(value.trim().to_string());
        }
    }
    None
}

fn new_token() -> String {
    let mut bytes = [0u8; 32];
    let mut file = OpenOptions::new()
        .read(true)
        .open("/dev/urandom")
        .expect("/dev/urandom");
    file.read_exact(&mut bytes).expect("random bytes");
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

fn now_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn response_with_headers(
    status: u16,
    reason: &str,
    headers: &[(&str, &str)],
    body: &str,
    content_type: &str,
) -> String {
    let mut out = format!(
        "HTTP/1.1 {} {}\r\nContent-Length: {}\r\nContent-Type: {}\r\nConnection: close\r\nX-Robots-Tag: noindex, nofollow, noarchive\r\n",
        status,
        reason,
        body.as_bytes().len(),
        content_type
    );
    for (k, v) in headers {
        out.push_str(k);
        out.push_str(": ");
        out.push_str(v);
        out.push_str("\r\n");
    }
    out.push_str("\r\n");
    out.push_str(body);
    out
}

fn html_response(status: u16, body: &str) -> String {
    let reason = if status == 200 { "OK" } else { "Unauthorized" };
    response_with_headers(status, reason, &[], body, "text/html; charset=utf-8")
}

fn empty_response(status: u16) -> String {
    let reason = if status == 204 { "No Content" } else { "OK" };
    response_with_headers(status, reason, &[], "", "text/plain; charset=utf-8")
}

fn redirect(location: &str) -> String {
    response_with_headers(
        303,
        "See Other",
        &[("Location", location)],
        "",
        "text/plain; charset=utf-8",
    )
}

fn not_found() -> String {
    response_with_headers(404, "Not Found", &[], "not found", "text/plain; charset=utf-8")
}

fn cookie_value(raw: &str, name: &str) -> Option<String> {
    for part in raw.split(';') {
        let part = part.trim();
        let (k, v) = part.split_once('=')?;
        if k.trim() == name {
            return Some(v.trim().to_string());
        }
    }
    None
}

fn parse_form(body: &str) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for pair in body.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        out.insert(url_decode(k), url_decode(v));
    }
    out
}

fn query_param(target: &str, key: &str) -> Option<String> {
    let query = target.split_once('?')?.1;
    parse_form(query).remove(key)
}

fn safe_next(value: &str) -> String {
    if value.starts_with('/') && !value.starts_with("//") {
        value.to_string()
    } else {
        "/".to_string()
    }
}

fn url_decode(value: &str) -> String {
    let mut out = Vec::new();
    let bytes = value.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b'%' if i + 2 < bytes.len() => {
                if let Ok(hex) = u8::from_str_radix(&value[i + 1..i + 3], 16) {
                    out.push(hex);
                    i += 3;
                } else {
                    out.push(bytes[i]);
                    i += 1;
                }
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

fn url_encode(value: &str) -> String {
    let mut out = String::new();
    for b in value.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~' | b'/') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}

fn escape_html(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|window| window == needle)
}
